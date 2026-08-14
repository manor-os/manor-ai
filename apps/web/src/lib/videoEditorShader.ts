export const VIDEO_EDITOR_SHADER_PRESETS = [
  "domainWarp",
  "liquidMetal",
  "volumetricFog",
  "chromaticTunnel",
  "inkBloom",
  "filmBurn",
  "prismaticBurst",
  "parallaxGrid",
] as const;

export type VideoEditorShaderPreset = (typeof VIDEO_EDITOR_SHADER_PRESETS)[number];

export type VideoEditorShaderStyle = {
  shaderPreset: VideoEditorShaderPreset;
  shaderSeed: number;
  shaderSpeed: number;
  shaderScale: number;
  shaderIntensity: number;
  shaderBloom: number;
  shaderGrain: number;
  shaderCameraX: number;
  shaderCameraY: number;
  shaderCameraZ: number;
};

export const DEFAULT_VIDEO_EDITOR_SHADER_STYLE: VideoEditorShaderStyle = {
  shaderPreset: "domainWarp",
  shaderSeed: 11,
  shaderSpeed: 1,
  shaderScale: 1,
  shaderIntensity: 1,
  shaderBloom: 0.45,
  shaderGrain: 0.08,
  shaderCameraX: 0,
  shaderCameraY: 0,
  shaderCameraZ: 1,
};

const finiteNumber = (value: unknown, fallback: number, minimum: number, maximum: number) => {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? Math.min(maximum, Math.max(minimum, parsed)) : fallback;
};

export const normalizeVideoEditorShaderStyle = (
  value: Partial<VideoEditorShaderStyle> | null | undefined,
): VideoEditorShaderStyle => ({
  shaderPreset: VIDEO_EDITOR_SHADER_PRESETS.includes(value?.shaderPreset as VideoEditorShaderPreset)
    ? (value?.shaderPreset as VideoEditorShaderPreset)
    : DEFAULT_VIDEO_EDITOR_SHADER_STYLE.shaderPreset,
  shaderSeed: Math.round(finiteNumber(value?.shaderSeed, DEFAULT_VIDEO_EDITOR_SHADER_STYLE.shaderSeed, 0, 9999)),
  shaderSpeed: finiteNumber(value?.shaderSpeed, DEFAULT_VIDEO_EDITOR_SHADER_STYLE.shaderSpeed, 0, 4),
  shaderScale: finiteNumber(value?.shaderScale, DEFAULT_VIDEO_EDITOR_SHADER_STYLE.shaderScale, 0.2, 5),
  shaderIntensity: finiteNumber(value?.shaderIntensity, DEFAULT_VIDEO_EDITOR_SHADER_STYLE.shaderIntensity, 0, 2),
  shaderBloom: finiteNumber(value?.shaderBloom, DEFAULT_VIDEO_EDITOR_SHADER_STYLE.shaderBloom, 0, 2),
  shaderGrain: finiteNumber(value?.shaderGrain, DEFAULT_VIDEO_EDITOR_SHADER_STYLE.shaderGrain, 0, 0.35),
  shaderCameraX: finiteNumber(value?.shaderCameraX, DEFAULT_VIDEO_EDITOR_SHADER_STYLE.shaderCameraX, -2, 2),
  shaderCameraY: finiteNumber(value?.shaderCameraY, DEFAULT_VIDEO_EDITOR_SHADER_STYLE.shaderCameraY, -2, 2),
  shaderCameraZ: finiteNumber(value?.shaderCameraZ, DEFAULT_VIDEO_EDITOR_SHADER_STYLE.shaderCameraZ, 0.25, 4),
});

const VERTEX_SHADER_SOURCE = `
attribute vec2 a_position;
void main() {
  gl_Position = vec4(a_position, 0.0, 1.0);
}
`;

const FRAGMENT_SHADER_SOURCE = `
precision highp float;

uniform vec2 u_resolution;
uniform float u_time;
uniform float u_duration;
uniform vec3 u_colorA;
uniform vec3 u_colorB;
uniform vec3 u_colorC;
uniform float u_preset;
uniform float u_seed;
uniform float u_speed;
uniform float u_scale;
uniform float u_intensity;
uniform float u_bloom;
uniform float u_grain;
uniform vec3 u_camera;

#define PI 3.14159265359

float hash21(vec2 p) {
  p = fract(p * vec2(123.34, 456.21));
  p += dot(p, p + 45.32 + u_seed * 0.017);
  return fract(p.x * p.y);
}

float noise(vec2 p) {
  vec2 i = floor(p);
  vec2 f = fract(p);
  f = f * f * (3.0 - 2.0 * f);
  return mix(mix(hash21(i), hash21(i + vec2(1.0, 0.0)), f.x),
             mix(hash21(i + vec2(0.0, 1.0)), hash21(i + vec2(1.0, 1.0)), f.x), f.y);
}

float fbm(vec2 p) {
  float value = 0.0;
  float amplitude = 0.52;
  mat2 rotation = mat2(0.80, 0.60, -0.60, 0.80);
  for (int octave = 0; octave < 6; octave++) {
    value += amplitude * noise(p);
    p = rotation * p * 2.03 + 13.7;
    amplitude *= 0.49;
  }
  return value;
}

vec3 domainWarp(vec2 p, float t) {
  vec2 q = vec2(fbm(p + vec2(0.0, t * 0.16)), fbm(p + vec2(5.2, 1.3) - t * 0.11));
  vec2 r = vec2(fbm(p + 3.8 * q + vec2(1.7, 9.2) + t * 0.13),
                fbm(p + 3.8 * q + vec2(8.3, 2.8) - t * 0.10));
  float field = fbm(p + 4.4 * r);
  float ridge = pow(1.0 - abs(2.0 * field - 1.0), 2.1);
  vec3 color = mix(u_colorA, u_colorB, smoothstep(0.12, 0.88, field));
  color = mix(color, u_colorC, smoothstep(0.48, 1.0, length(r) * 0.72));
  return color + u_colorC * ridge * (0.22 + u_bloom * 0.34);
}

vec3 liquidMetal(vec2 p, float t) {
  vec2 flow = p * 1.22;
  flow.x += sin(flow.y * 2.1 + t * 0.31) * 0.24;
  flow.y += cos(flow.x * 1.7 - t * 0.27) * 0.18;
  float e = 0.012;
  float height = fbm(flow * 1.7 + t * 0.08);
  float dx = fbm((flow + vec2(e, 0.0)) * 1.7 + t * 0.08) - height;
  float dy = fbm((flow + vec2(0.0, e)) * 1.7 + t * 0.08) - height;
  vec3 normal = normalize(vec3(-dx * 38.0, -dy * 38.0, 1.0));
  vec3 lightA = normalize(vec3(-0.55 + sin(t * 0.2) * 0.2, -0.35, 0.9));
  vec3 lightB = normalize(vec3(0.65, 0.45, 0.7));
  float specular = pow(max(dot(normal, lightA), 0.0), 20.0) + 0.38 * pow(max(dot(normal, lightB), 0.0), 48.0);
  float fresnel = pow(1.0 - max(normal.z, 0.0), 2.4);
  vec3 base = mix(u_colorA, u_colorB, smoothstep(0.18, 0.82, height));
  return base * (0.46 + normal.z * 0.68) + u_colorC * (specular * (0.9 + u_bloom) + fresnel * 0.48);
}

vec3 volumetricFog(vec2 p, float t) {
  vec3 color = u_colorA * 0.52;
  float fog = 0.0;
  for (int layer = 0; layer < 6; layer++) {
    float depth = float(layer) / 5.0;
    vec2 drift = vec2(t * (0.025 + depth * 0.026), -t * (0.014 + depth * 0.012));
    float density = fbm(p * (1.15 + depth * 2.8) + drift + float(layer) * 7.1);
    fog += smoothstep(0.42 + depth * 0.05, 0.86, density) * (0.22 - depth * 0.02);
  }
  float beam = pow(max(0.0, 1.0 - abs(p.x + p.y * 0.28 + sin(t * 0.11) * 0.18)), 4.5);
  float halo = exp(-2.8 * length(p - vec2(-0.35, -0.22)));
  color = mix(color, u_colorB, clamp(fog * 0.82, 0.0, 1.0));
  color += u_colorC * (beam * (0.18 + u_bloom * 0.2) + halo * 0.23);
  return color;
}

vec3 chromaticTunnel(vec2 p, float t) {
  float radius = max(length(p), 0.001);
  float angle = atan(p.y, p.x);
  float tunnel = 1.0 / radius + t * 0.22;
  float bands = 0.5 + 0.5 * sin(tunnel * 8.0 + angle * 3.0 + fbm(p * 3.0) * 3.2);
  float spokes = 0.5 + 0.5 * cos(angle * 7.0 - t * 0.4 + radius * 8.0);
  float pulse = smoothstep(0.48, 0.9, bands * 0.74 + spokes * 0.32);
  vec3 color = mix(u_colorA, u_colorB, bands);
  color = mix(color, u_colorC, pulse * (0.72 + u_bloom * 0.18));
  return color * (0.48 + min(1.35, 0.38 / radius));
}

vec3 inkBloom(vec2 p, float t) {
  float progress = clamp(u_time / max(u_duration, 0.001), 0.0, 1.0);
  vec2 drift = vec2(sin(t * 0.11), cos(t * 0.09)) * 0.16;
  float coarse = fbm(p * 1.18 + drift);
  float detail = fbm(p * 3.7 - drift * 1.6 + coarse * 1.9);
  float distanceField = length(p * vec2(0.82, 1.0)) - progress * 1.72;
  float ink = 1.0 - smoothstep(-0.24, 0.16, distanceField + (coarse - 0.5) * 0.64 + (detail - 0.5) * 0.18);
  float wetEdge = 1.0 - smoothstep(0.025, 0.16, abs(distanceField + (coarse - 0.5) * 0.64));
  vec3 paper = mix(u_colorA, u_colorB, coarse * 0.2);
  vec3 pigment = mix(u_colorB, u_colorC, smoothstep(0.22, 0.92, detail));
  return mix(paper, pigment, ink * (0.72 + u_intensity * 0.2)) + u_colorC * wetEdge * u_bloom * 0.25;
}

vec3 filmBurn(vec2 p, float t) {
  float progress = clamp(u_time / max(u_duration, 0.001), 0.0, 1.0);
  vec2 burnOrigin = vec2(-0.95 + progress * 1.9, sin(t * 0.19) * 0.32);
  float distortion = (fbm(p * 2.35 + t * 0.07) - 0.5) * 0.58;
  float burnDistance = length((p - burnOrigin) * vec2(0.72, 1.12)) + distortion;
  float core = 1.0 - smoothstep(0.08, 0.72, burnDistance);
  float ember = 1.0 - smoothstep(0.04, 0.17, abs(burnDistance - 0.42));
  float soot = smoothstep(0.18, 0.62, fbm(p * 5.4 + t * 0.13));
  vec3 base = mix(u_colorA, u_colorB, soot * 0.44);
  base = mix(base, u_colorC, ember * (0.72 + u_bloom * 0.34));
  return base + u_colorC * core * (0.55 + u_intensity * 0.36);
}

vec3 prismaticBurst(vec2 p, float t) {
  float radius = max(length(p), 0.001);
  float angle = atan(p.y, p.x);
  float facets = abs(sin(angle * 6.0 + fbm(p * 2.1) * 2.2 - t * 0.24));
  float rays = pow(max(0.0, cos(angle * 12.0 + t * 0.31)), 18.0);
  float wave = 0.5 + 0.5 * sin(radius * 15.0 - t * 1.9 + facets * 2.8);
  float caustic = smoothstep(0.54, 0.96, facets * 0.58 + wave * 0.6);
  vec3 color = mix(u_colorA, u_colorB, wave);
  color = mix(color, u_colorC, caustic * (0.7 + u_bloom * 0.2));
  color += u_colorC * rays * (0.3 + u_bloom * 0.48) / (0.5 + radius);
  return color * (0.66 + 0.42 / (0.42 + radius));
}

vec3 parallaxGrid(vec2 p, float t) {
  float depth = fract(0.18 / max(abs(p.y + 0.42), 0.035) + t * 0.18);
  float horizon = exp(-pow(abs(p.y + 0.36) * 6.5, 2.0));
  float perspectiveX = p.x / max(abs(p.y + 0.43), 0.075);
  float vertical = 1.0 - smoothstep(0.018, 0.055, abs(fract(perspectiveX * 0.14 + 0.5) - 0.5));
  float horizontal = 1.0 - smoothstep(0.025, 0.075, abs(depth - 0.5));
  float grid = max(vertical, horizontal) * smoothstep(1.28, 0.12, abs(p.y));
  float pulse = 0.5 + 0.5 * sin(t * 0.72 + length(p) * 7.0);
  vec3 color = mix(u_colorA, u_colorB, horizon * 0.44 + pulse * 0.1);
  color += mix(u_colorB, u_colorC, pulse) * grid * (0.52 + u_bloom * 0.5);
  color += u_colorC * horizon * (0.14 + u_bloom * 0.22);
  return color;
}

void main() {
  vec2 uv = gl_FragCoord.xy / u_resolution.xy;
  vec2 p = (gl_FragCoord.xy * 2.0 - u_resolution.xy) / min(u_resolution.x, u_resolution.y);
  p = (p - u_camera.xy * 0.22) * u_scale / max(u_camera.z, 0.05);
  float t = u_time * u_speed + u_seed * 0.071;
  vec3 color;
  if (u_preset < 0.5) {
    color = domainWarp(p, t);
  } else if (u_preset < 1.5) {
    color = liquidMetal(p, t);
  } else if (u_preset < 2.5) {
    color = volumetricFog(p, t);
  } else if (u_preset < 3.5) {
    color = chromaticTunnel(p, t);
  } else if (u_preset < 4.5) {
    color = inkBloom(p, t);
  } else if (u_preset < 5.5) {
    color = filmBurn(p, t);
  } else if (u_preset < 6.5) {
    color = prismaticBurst(p, t);
  } else {
    color = parallaxGrid(p, t);
  }
  float vignette = smoothstep(1.32, 0.22, length((uv - 0.5) * vec2(u_resolution.x / u_resolution.y, 1.0)));
  color *= mix(0.50, 1.06, vignette);
  color *= 0.72 + u_intensity * 0.36;
  color += max(color - 0.72, 0.0) * u_bloom * 0.48;
  float grain = hash21(gl_FragCoord.xy + floor(u_time * 30.0) * vec2(17.0, 31.0)) - 0.5;
  color += grain * u_grain;
  color = vec3(1.0) - exp(-max(color, 0.0) * (1.08 + u_intensity * 0.16));
  color = pow(max(color, 0.0), vec3(0.94));
  gl_FragColor = vec4(color, 1.0);
}
`;

const parseHexColor = (value: string, fallback: [number, number, number]): [number, number, number] => {
  const normalized = value.trim();
  const shorthand = /^#([\da-f])([\da-f])([\da-f])$/i.exec(normalized);
  const expanded = shorthand ? `#${shorthand[1]}${shorthand[1]}${shorthand[2]}${shorthand[2]}${shorthand[3]}${shorthand[3]}` : normalized;
  const match = /^#([\da-f]{2})([\da-f]{2})([\da-f]{2})$/i.exec(expanded);
  if (!match) return fallback;
  return [Number.parseInt(match[1], 16) / 255, Number.parseInt(match[2], 16) / 255, Number.parseInt(match[3], 16) / 255];
};

const createShader = (gl: WebGLRenderingContext, type: number, source: string) => {
  const shader = gl.createShader(type);
  if (!shader) throw new Error("Unable to create WebGL shader.");
  gl.shaderSource(shader, source);
  gl.compileShader(shader);
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
    const message = gl.getShaderInfoLog(shader) || "Unknown WebGL shader compilation error.";
    gl.deleteShader(shader);
    throw new Error(message);
  }
  return shader;
};

export type VideoEditorShaderSurface = {
  canvas: HTMLCanvasElement;
  gl: WebGLRenderingContext | null;
  program: WebGLProgram | null;
  positionBuffer: WebGLBuffer | null;
  fallbackContext: CanvasRenderingContext2D | null;
};

export const createVideoEditorShaderSurface = (
  width: number,
  height: number,
  canvas = document.createElement("canvas"),
): VideoEditorShaderSurface => {
  canvas.width = Math.max(2, Math.round(width));
  canvas.height = Math.max(2, Math.round(height));
  const gl = canvas.getContext("webgl", {
    alpha: false,
    antialias: false,
    depth: false,
    premultipliedAlpha: false,
    preserveDrawingBuffer: true,
    stencil: false,
  });
  if (!gl) {
    return { canvas, gl: null, program: null, positionBuffer: null, fallbackContext: canvas.getContext("2d") };
  }
  try {
    const vertexShader = createShader(gl, gl.VERTEX_SHADER, VERTEX_SHADER_SOURCE);
    const fragmentShader = createShader(gl, gl.FRAGMENT_SHADER, FRAGMENT_SHADER_SOURCE);
    const program = gl.createProgram();
    if (!program) throw new Error("Unable to create WebGL program.");
    gl.attachShader(program, vertexShader);
    gl.attachShader(program, fragmentShader);
    gl.linkProgram(program);
    gl.deleteShader(vertexShader);
    gl.deleteShader(fragmentShader);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
      throw new Error(gl.getProgramInfoLog(program) || "Unknown WebGL program link error.");
    }
    const positionBuffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, positionBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, -1, 1, 1, -1, 1, 1]), gl.STATIC_DRAW);
    return { canvas, gl, program, positionBuffer, fallbackContext: null };
  } catch {
    return { canvas, gl: null, program: null, positionBuffer: null, fallbackContext: canvas.getContext("2d") };
  }
};

export const resizeVideoEditorShaderSurface = (surface: VideoEditorShaderSurface, width: number, height: number) => {
  const nextWidth = Math.max(2, Math.round(width));
  const nextHeight = Math.max(2, Math.round(height));
  if (surface.canvas.width !== nextWidth) surface.canvas.width = nextWidth;
  if (surface.canvas.height !== nextHeight) surface.canvas.height = nextHeight;
};

type RenderShaderFrameOptions = {
  time: number;
  duration: number;
  colorA: string;
  colorB: string;
  colorC: string;
  style?: Partial<VideoEditorShaderStyle> | null;
};

const renderFallbackFrame = (
  surface: VideoEditorShaderSurface,
  options: RenderShaderFrameOptions,
  style: VideoEditorShaderStyle,
) => {
  const context = surface.fallbackContext;
  if (!context) return;
  const { width, height } = surface.canvas;
  const phase = options.time * style.shaderSpeed + style.shaderSeed * 0.071;
  const centerX = width * (0.5 + style.shaderCameraX * 0.08 + Math.sin(phase * 0.31) * 0.12);
  const centerY = height * (0.5 + style.shaderCameraY * 0.08 + Math.cos(phase * 0.27) * 0.11);
  const radius = Math.hypot(width, height) * (0.36 + 0.12 / style.shaderCameraZ);
  context.clearRect(0, 0, width, height);
  const gradient = context.createRadialGradient(centerX, centerY, 0, centerX, centerY, radius);
  gradient.addColorStop(0, options.colorC);
  gradient.addColorStop(0.42, options.colorB);
  gradient.addColorStop(1, options.colorA);
  context.fillStyle = gradient;
  context.fillRect(0, 0, width, height);
  context.globalCompositeOperation = "screen";
  context.globalAlpha = Math.min(0.7, 0.18 + style.shaderBloom * 0.22);
  for (let index = 0; index < 7; index += 1) {
    const angle = phase * (0.08 + index * 0.011) + index * 1.71;
    const x = centerX + Math.cos(angle) * width * (0.08 + index * 0.045);
    const y = centerY + Math.sin(angle * 1.23) * height * (0.07 + index * 0.038);
    context.beginPath();
    context.arc(x, y, radius * (0.15 + index * 0.035), 0, Math.PI * 2);
    context.fillStyle = index % 2 ? options.colorB : options.colorC;
    context.fill();
  }
  context.globalCompositeOperation = "source-over";
  context.globalAlpha = 1;
};

export const renderVideoEditorShaderFrame = (
  surface: VideoEditorShaderSurface,
  options: RenderShaderFrameOptions,
) => {
  const style = normalizeVideoEditorShaderStyle(options.style);
  const { gl, program } = surface;
  if (!gl || !program) {
    renderFallbackFrame(surface, options, style);
    return;
  }
  gl.viewport(0, 0, surface.canvas.width, surface.canvas.height);
  gl.useProgram(program);
  gl.bindBuffer(gl.ARRAY_BUFFER, surface.positionBuffer);
  const position = gl.getAttribLocation(program, "a_position");
  gl.enableVertexAttribArray(position);
  gl.vertexAttribPointer(position, 2, gl.FLOAT, false, 0, 0);
  const uniform1f = (name: string, value: number) => gl.uniform1f(gl.getUniformLocation(program, name), value);
  const uniform2f = (name: string, x: number, y: number) => gl.uniform2f(gl.getUniformLocation(program, name), x, y);
  const uniform3f = (name: string, values: [number, number, number]) => gl.uniform3f(gl.getUniformLocation(program, name), ...values);
  uniform2f("u_resolution", surface.canvas.width, surface.canvas.height);
  uniform1f("u_time", Math.max(0, options.time));
  uniform1f("u_duration", Math.max(0.001, options.duration));
  uniform3f("u_colorA", parseHexColor(options.colorA, [0.025, 0.035, 0.055]));
  uniform3f("u_colorB", parseHexColor(options.colorB, [0.34, 0.18, 0.8]));
  uniform3f("u_colorC", parseHexColor(options.colorC, [0.98, 0.66, 0.24]));
  uniform1f("u_preset", VIDEO_EDITOR_SHADER_PRESETS.indexOf(style.shaderPreset));
  uniform1f("u_seed", style.shaderSeed);
  uniform1f("u_speed", style.shaderSpeed);
  uniform1f("u_scale", style.shaderScale);
  uniform1f("u_intensity", style.shaderIntensity);
  uniform1f("u_bloom", style.shaderBloom);
  uniform1f("u_grain", style.shaderGrain);
  gl.uniform3f(gl.getUniformLocation(program, "u_camera"), style.shaderCameraX, style.shaderCameraY, style.shaderCameraZ);
  gl.drawArrays(gl.TRIANGLES, 0, 6);
};

export const disposeVideoEditorShaderSurface = (surface: VideoEditorShaderSurface) => {
  if (!surface.gl) return;
  if (surface.positionBuffer) surface.gl.deleteBuffer(surface.positionBuffer);
  if (surface.program) surface.gl.deleteProgram(surface.program);
};
