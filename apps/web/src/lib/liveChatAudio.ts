/** Duplex audio, with a single clock for playback and interruption truncation. */
export class LiveChatAudio {
  private context = new AudioContext();
  private stream?: MediaStream;
  private source?: MediaStreamAudioSourceNode;
  private capture?: AudioWorkletNode;
  private silent?: GainNode;
  private closed = false;
  private muted = false;
  private nodes = new Map<AudioBufferSourceNode, {
    buffer: AudioBuffer; gain: GainNode; itemId: string; at: number; offset: number;
  }>();
  private pausedAt?: number;
  private pausedClips: { buffer: AudioBuffer; itemId: string; offset: number }[] = [];
  private nextAt = 0;
  private playback?: { itemId: string; startsAt: number; endsAt: number; done: boolean };
  private playbackEpoch = 0;
  private decodeQueue: Promise<unknown> = Promise.resolve();
  private duplexMode: "full" | "push_to_interrupt" = "full";
  private captureSuppressedUntil = 0;

  async start(onAudio: (audio: string) => void, onPlaybackEnd: (itemId?: string) => void, onFailure: () => void,
    onInputLevel?: (level: number, device: string) => void) {
    // Resume synchronously from the call button's user gesture (including iOS).
    await this.context.resume();
    const supported = navigator.mediaDevices.getSupportedConstraints?.() || {};
    const constraints: MediaTrackConstraints & { voiceIsolation?: boolean } = {
      channelCount: 1,
      echoCancellation: true,
      noiseSuppression: true,
      // AGC raises distant room audio during pauses and makes it look like a
      // nearby speaker. Keep the physical level difference for server VAD.
      autoGainControl: false,
    };
    if ((supported as MediaTrackSupportedConstraints & { voiceIsolation?: boolean }).voiceIsolation) {
      constraints.voiceIsolation = true;
    }
    const stream = await navigator.mediaDevices.getUserMedia({ audio: constraints });
    if (this.closed) { stream.getTracks().forEach(track => track.stop()); return; }
    this.stream = stream;
    stream.getAudioTracks().forEach(track => { track.onended = onFailure; });
    await this.context.audioWorklet.addModule("/audio/voice-capture.js");
    if (this.closed) return;
    this.source = this.context.createMediaStreamSource(stream);
    this.capture = new AudioWorkletNode(this.context, "manor-voice-capture");
    this.silent = this.context.createGain();
    this.silent.gain.value = 0;
    this.source.connect(this.capture).connect(this.silent).connect(this.context.destination);
    let lastLevelAt = 0;
    this.capture.port.onmessage = ({ data }: MessageEvent<ArrayBuffer>) => {
      if (this.closed || this.muted) return;
      if (onInputLevel && performance.now() - lastLevelAt >= 100) {
        const samples = new Int16Array(data);
        const energy = samples.reduce((sum, sample) => sum + (sample / 32768) ** 2, 0);
        onInputLevel(Math.min(1, Math.sqrt(energy / samples.length) * 5), stream.getAudioTracks()[0]?.label || "");
        lastLevelAt = performance.now();
      }
      // The shared STT/Chat/TTS fallback cannot cancel acoustic echo as
      // reliably as a native Realtime connection. While its reply is
      // playing, keep the microphone meter live but do not upload the reply
      // back to STT as a fake user interruption. The call UI exposes an
      // explicit interrupt action that clears playback before capture resumes.
      if (
        this.duplexMode === "push_to_interrupt"
        && (this.playback !== undefined || performance.now() < this.captureSuppressedUntil)
      ) return;
      const bytes = new Uint8Array(data);
      onAudio(btoa(String.fromCharCode(...bytes)));
    };
    this.onPlaybackEnd = onPlaybackEnd;
  }

  private onPlaybackEnd: (itemId?: string) => void = () => {};

  play(audio: string, itemId: string) {
    if (this.closed) return;
    if (this.context.state !== "running") throw new Error("Audio playback was suspended. Start the call again.");
    const bytes = Uint8Array.from(atob(audio), char => char.charCodeAt(0));
    const view = new DataView(bytes.buffer);
    const buffer = this.context.createBuffer(1, bytes.byteLength / 2, 24000);
    const channel = buffer.getChannelData(0);
    for (let i = 0; i < channel.length; i++) channel[i] = view.getInt16(i * 2, true) / 32768;
    this.enqueue(buffer, itemId);
  }

  playClip(audio: string, itemId: string): Promise<boolean> {
    const epoch = this.playbackEpoch;
    const pending = this.decodeQueue.then(async () => {
      if (this.closed || epoch !== this.playbackEpoch) return false;
      const bytes = Uint8Array.from(atob(audio), char => char.charCodeAt(0));
      const buffer = await this.context.decodeAudioData(bytes.buffer);
      // Decoding can finish after an interruption or hangup. Never enqueue
      // that stale clip, even if newer audio is already being played.
      if (this.closed || epoch !== this.playbackEpoch) return false;
      this.enqueue(buffer, itemId);
      this.finish(itemId);
      return this.pausedAt === undefined;
    });
    this.decodeQueue = pending.catch(() => {});
    return pending;
  }

  private enqueue(buffer: AudioBuffer, itemId: string, offset = 0) {
    if (this.context.state !== "running") throw new Error("Audio playback was suspended. Start the call again.");
    const at = Math.max(this.context.currentTime + 0.02, this.nextAt);
    if (at - this.context.currentTime > 120) throw new Error("Voice playback buffer is full");
    if (this.playback?.itemId !== itemId) this.playback = { itemId, startsAt: at, endsAt: at, done: false };
    if (this.pausedAt !== undefined) {
      this.pausedClips.push({ buffer, itemId, offset });
      return;
    }
    this.nextAt = at + buffer.duration - offset;
    this.playback.endsAt = this.nextAt;
    const node = this.context.createBufferSource();
    const gain = this.context.createGain();
    node.buffer = buffer;
    gain.gain.value = this.playbackGain(buffer);
    node.connect(gain).connect(this.context.destination);
    this.nodes.set(node, { buffer, gain, itemId, at, offset });
    node.onended = () => {
      const active = this.nodes.get(node);
      this.nodes.delete(node);
      node.disconnect(); active?.gain.disconnect();
      if (!this.nodes.size && this.playback?.done) this.completePlayback(this.playback.itemId);
    };
    node.start(at, offset);
  }

  private playbackGain(buffer: AudioBuffer) {
    let peak = 0;
    for (let channel = 0; channel < buffer.numberOfChannels; channel++) {
      const samples = buffer.getChannelData(channel);
      for (let index = 0; index < samples.length; index += 4) peak = Math.max(peak, Math.abs(samples[index]));
    }
    if (peak <= 0 || peak >= 0.82) return 1;
    return Math.min(2.5, 0.82 / peak);
  }

  private completePlayback(itemId: string) {
    if (this.playback?.itemId !== itemId) return;
    this.playback = undefined;
    this.nextAt = 0;
    // Do not upload the short acoustic tail left in the room after playback.
    this.captureSuppressedUntil = performance.now() + 240;
    this.onPlaybackEnd(itemId);
  }

  finish(itemId: string) {
    if (this.playback?.itemId !== itemId) return;
    this.playback.done = true;
    if (!this.nodes.size && this.pausedAt === undefined) this.completePlayback(itemId);
  }

  pause() {
    if (this.closed || this.pausedAt !== undefined) return;
    this.pausedAt = this.context.currentTime;
    for (const [node, clip] of this.nodes) {
      const offset = clip.offset + Math.max(0, this.pausedAt - clip.at);
      if (offset < clip.buffer.duration) this.pausedClips.push({ ...clip, offset });
      node.onended = null; node.stop(); node.disconnect(); clip.gain.disconnect();
    }
    this.nodes.clear(); this.nextAt = 0;
  }

  resume() {
    if (this.closed || this.pausedAt === undefined) return false;
    if (this.playback) this.playback.startsAt += this.context.currentTime - this.pausedAt;
    this.pausedAt = undefined;
    const clips = this.pausedClips; this.pausedClips = [];
    for (const clip of clips) this.enqueue(clip.buffer, clip.itemId, clip.offset);
    if (!this.nodes.size && this.playback?.done) this.completePlayback(this.playback.itemId);
    return this.nodes.size > 0;
  }

  interrupt() {
    this.playbackEpoch++;
    const playback = this.playback;
    const milliseconds = playback
      ? Math.floor(Math.max(0, Math.min(this.pausedAt ?? this.context.currentTime, playback.endsAt) - playback.startsAt) * 1000) : 0;
    this.playback = undefined;
    this.nextAt = 0;
    for (const [node, clip] of this.nodes) {
      node.onended = null; node.stop(); node.disconnect(); clip.gain.disconnect();
    }
    this.nodes.clear();
    this.pausedClips = []; this.pausedAt = undefined;
    this.captureSuppressedUntil = performance.now() + 240;
    return { item_id: playback?.itemId, audio_end_ms: milliseconds };
  }

  setDuplexMode(mode: "full" | "push_to_interrupt") {
    this.duplexMode = mode;
  }

  setMuted(muted: boolean) {
    this.muted = muted;
    this.stream?.getAudioTracks().forEach(track => { track.enabled = !muted; });
  }

  close() {
    this.closed = true;
    this.interrupt();
    this.stream?.getTracks().forEach(track => { track.onended = null; track.stop(); });
    this.source?.disconnect();
    if (this.capture) { this.capture.port.onmessage = null; this.capture.disconnect(); }
    this.silent?.disconnect();
    void this.context.close().catch(() => {});
  }
}
