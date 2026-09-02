// Capture mono PCM16 at 24 kHz, independent of the hardware sample rate.
class ManorVoiceCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.samples = [];
    this.position = 0;
    this.chunk = new Int16Array(960);
    this.offset = 0;
  }
  process(inputs) {
    const input = inputs[0]?.[0];
    if (!input) return true;
    for (const sample of input) this.samples.push(sample);
    const step = sampleRate / 24000;
    while (this.position + 1 < this.samples.length) {
      const index = Math.floor(this.position);
      const fraction = this.position - index;
      const value = Math.max(-1, Math.min(1,
        this.samples[index] * (1 - fraction) + this.samples[index + 1] * fraction));
      this.chunk[this.offset++] = value < 0 ? value * 32768 : value * 32767;
      this.position += step;
      if (this.offset === this.chunk.length) {
        this.port.postMessage(this.chunk.buffer, [this.chunk.buffer]);
        this.chunk = new Int16Array(960);
        this.offset = 0;
      }
    }
    const consumed = Math.floor(this.position);
    this.samples.splice(0, consumed);
    this.position -= consumed;
    return true;
  }
}
registerProcessor("manor-voice-capture", ManorVoiceCapture);
