// CSP has no working directive for WebRTC (`webrtc 'block'` has no effect in
// Chromium), so documents that may run generated code delete the constructors
// before anything generated can capture them.
export const REMOVE_PEER_CONNECTION_CONSTRUCTORS = `for (const name of ["RTCPeerConnection", "webkitRTCPeerConnection", "RTCDataChannel"]) delete window[name];`;

const ROUND_CONSTANTS = new Uint32Array([
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
  0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
  0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
  0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
  0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
  0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
]);

const rotateRight = (value: number, bits: number) => (value >>> bits) | (value << (32 - bits));

function sha256(message: Uint8Array): Uint8Array {
  const paddedLength = ((message.length + 9 + 63) >> 6) << 6;
  const padded = new Uint8Array(paddedLength);
  padded.set(message);
  padded[message.length] = 0x80;
  const view = new DataView(padded.buffer);
  view.setUint32(paddedLength - 8, Math.floor(message.length / 0x20000000));
  view.setUint32(paddedLength - 4, message.length << 3);

  const state = new Uint32Array([
    0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
  ]);
  const schedule = new Uint32Array(64);
  for (let offset = 0; offset < paddedLength; offset += 64) {
    for (let i = 0; i < 16; i += 1) schedule[i] = view.getUint32(offset + i * 4);
    for (let i = 16; i < 64; i += 1) {
      const s0 = rotateRight(schedule[i - 15], 7) ^ rotateRight(schedule[i - 15], 18) ^ (schedule[i - 15] >>> 3);
      const s1 = rotateRight(schedule[i - 2], 17) ^ rotateRight(schedule[i - 2], 19) ^ (schedule[i - 2] >>> 10);
      schedule[i] = schedule[i - 16] + s0 + schedule[i - 7] + s1;
    }
    let [a, b, c, d, e, f, g, h] = state;
    for (let i = 0; i < 64; i += 1) {
      const s1 = rotateRight(e, 6) ^ rotateRight(e, 11) ^ rotateRight(e, 25);
      const choice = (e & f) ^ (~e & g);
      const temp1 = (h + s1 + choice + ROUND_CONSTANTS[i] + schedule[i]) | 0;
      const s0 = rotateRight(a, 2) ^ rotateRight(a, 13) ^ rotateRight(a, 22);
      const majority = (a & b) ^ (a & c) ^ (b & c);
      h = g;
      g = f;
      f = e;
      e = (d + temp1) | 0;
      d = c;
      c = b;
      b = a;
      a = (temp1 + s0 + majority) | 0;
    }
    [a, b, c, d, e, f, g, h].forEach((value, index) => {
      state[index] += value;
    });
  }
  const digest = new DataView(new ArrayBuffer(32));
  state.forEach((value, index) => digest.setUint32(index * 4, value));
  return new Uint8Array(digest.buffer);
}

/** Prepares inline script text for hashing: the HTML parser rewrites these characters before the browser hashes it. */
export function inlineScriptTextAsParsed(script: string): string {
  return script.replace(/\r\n?/g, "\n").replace(/\0/g, "�");
}

/**
 * The `script-src` source for exactly this inline script. Unlike a nonce it
 * holds no secret, so it stays safe when generated code reads the policy, and
 * a child frame that inherits it can only replay this same script.
 */
export function inlineScriptHashSource(script: string): string {
  const digest = sha256(new TextEncoder().encode(script));
  return `'sha256-${btoa(String.fromCharCode(...digest))}'`;
}
