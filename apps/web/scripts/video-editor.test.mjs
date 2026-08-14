import assert from "node:assert/strict";
import { build } from "esbuild";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { pathToFileURL } from "node:url";
import os from "node:os";
import path from "node:path";

const root = path.resolve(import.meta.dirname, "../../..");
const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), "manor-video-editor-"));
const geometryOutput = path.join(temporaryDirectory, "geometry.mjs");
const motionOutput = path.join(temporaryDirectory, "motion.mjs");
const timelineOutput = path.join(temporaryDirectory, "timeline.mjs");
const motionDesignOutput = path.join(temporaryDirectory, "motion-design.mjs");
const visualStyleOutput = path.join(temporaryDirectory, "visual-style.mjs");
const particlesOutput = path.join(temporaryDirectory, "particles.mjs");
const shaderOutput = path.join(temporaryDirectory, "shader.mjs");

try {
  await build({
    entryPoints: [path.join(root, "apps/web/src/lib/videoEditorGeometry.ts")],
    outfile: geometryOutput,
    bundle: true,
    platform: "node",
    format: "esm",
  });
  await build({
    entryPoints: [path.join(root, "apps/web/src/lib/videoEditorMotion.ts")],
    outfile: motionOutput,
    bundle: true,
    platform: "node",
    format: "esm",
  });
  await build({
    entryPoints: [path.join(root, "apps/web/src/lib/videoEditorTimeline.ts")],
    outfile: timelineOutput,
    bundle: true,
    platform: "node",
    format: "esm",
  });
  await build({
    entryPoints: [path.join(root, "apps/web/src/lib/videoEditorMotionDesign.ts")],
    outfile: motionDesignOutput,
    bundle: true,
    platform: "node",
    format: "esm",
  });
  await build({
    entryPoints: [path.join(root, "apps/web/src/lib/videoEditorVisualStyle.ts")],
    outfile: visualStyleOutput,
    bundle: true,
    platform: "node",
    format: "esm",
  });
  await build({
    entryPoints: [path.join(root, "apps/web/src/lib/videoEditorParticles.ts")],
    outfile: particlesOutput,
    bundle: true,
    platform: "node",
    format: "esm",
  });
  await build({
    entryPoints: [path.join(root, "apps/web/src/lib/videoEditorShader.ts")],
    outfile: shaderOutput,
    bundle: true,
    platform: "node",
    format: "esm",
  });
  const geometry = await import(`${pathToFileURL(geometryOutput).href}?${Date.now()}`);
  const motion = await import(`${pathToFileURL(motionOutput).href}?${Date.now()}`);
  const timeline = await import(`${pathToFileURL(timelineOutput).href}?${Date.now()}`);
  const motionDesign = await import(`${pathToFileURL(motionDesignOutput).href}?${Date.now()}`);
  const visualStyle = await import(`${pathToFileURL(visualStyleOutput).href}?${Date.now()}`);
  const particles = await import(`${pathToFileURL(particlesOutput).href}?${Date.now()}`);
  const shader = await import(`${pathToFileURL(shaderOutput).href}?${Date.now()}`);
  assert.deepEqual(geometry.containedMediaSize({ width: 1200, height: 600 }, { width: 1920, height: 1080 }), { width: 1066.6666666666667, height: 600 });
  assert.deepEqual(geometry.containedMediaSize({ width: 1000, height: 600 }, { width: 1080, height: 1920 }), { width: 337.5, height: 600 });
  assert.deepEqual(geometry.fittedMediaSize({ width: 1280, height: 720 }, { width: 1080, height: 1920 }, "contain"), { width: 405, height: 720 });
  assert.deepEqual(geometry.fittedMediaSize({ width: 1280, height: 720 }, { width: 1080, height: 1920 }, "cover"), { width: 1280, height: 2275.5555555555557 });
  assert.equal(geometry.captionAnchorTransform("center", 48), "translate(-50%, -50%)");
  assert.equal(geometry.captionAnchorTransform("left", 48), "translate(-24px, -50%)");
  assert.equal(geometry.captionAnchorTransform("right", 48), "translate(calc(-100% + 24px), -50%)");

  const fallback = { x: 50, y: 84, scale: 1, rotation: 0, opacity: 1 };
  const normalized = motion.normalizeMotionKeyframes([
    { id: "end", time: 2, x: 70, y: 50, scale: 2, rotation: 90, opacity: 0.5, easing: "linear" },
    { id: "start", time: 0, x: 30, y: 70, scale: 1, rotation: 0, opacity: 1, easing: "linear" },
  ], fallback, 3);
  assert.deepEqual(normalized.map((frame) => frame.id), ["start", "end"]);
  assert.deepEqual(motion.motionPoseAtTime(normalized, 1, fallback), {
    x: 50,
    y: 60,
    scale: 1.5,
    rotation: 45,
    opacity: 0.75,
  });
  assert.deepEqual(motion.motionPoseAtTime(normalized, -1, fallback), {
    x: 30,
    y: 70,
    scale: 1,
    rotation: 0,
    opacity: 1,
  });
  assert.deepEqual(motion.MOTION_EASINGS, [
    "linear",
    "hold",
    "gentle",
    "easeIn",
    "easeOut",
    "easeInOut",
    "snappy",
    "spring",
    "custom",
  ]);
  assert.equal(motion.motionEasingProgress("linear", 0.25), 0.25);
  assert.equal(motion.motionEasingProgress("hold", 0.999), 0);
  assert.equal(motion.motionEasingProgress("hold", 1), 1);
  assert.equal(Math.abs(motion.motionEasingProgress("gentle", 0.5) - 0.5) < 1e-9, true);
  assert.equal(motion.motionEasingProgress("easeIn", 0.5), 0.125);
  assert.equal(motion.motionEasingProgress("easeOut", 0.5), 0.875);
  assert.equal(motion.motionEasingProgress("snappy", 0.5), 0.9375);
  assert.equal(motion.motionEasingProgress("spring", 0.5) > 0.9, true);
  assert.equal(Math.abs(motion.motionEasingProgress("custom", 0.25, {
    x1: 0,
    y1: 0,
    x2: 1,
    y2: 1,
  }) - 0.25) < 1e-6, true);
  assert.deepEqual(motion.normalizeMotionBezier({ x1: -2, y1: -3, x2: 4, y2: 5 }), {
    x1: 0,
    y1: -1,
    x2: 1,
    y2: 2,
  });
  const curvedFrames = motion.normalizeMotionKeyframes([
    { ...fallback, id: "curve-start", time: 0, x: 20, y: 50, easing: "linear", spatial: "linear" },
    { ...fallback, id: "curve-middle", time: 1, x: 50, y: 20, easing: "linear", spatial: "smooth" },
    { ...fallback, id: "curve-end", time: 2, x: 80, y: 50, easing: "linear", spatial: "smooth" },
  ], fallback, 2);
  const curvedPath = motion.motionPathSamples(curvedFrames, fallback, 4);
  assert.equal(curvedPath.length, 9);
  assert.deepEqual(curvedPath[0], { time: 0, x: 20, y: 50 });
  assert.deepEqual(curvedPath[4], { time: 1, x: 50, y: 20 });
  assert.deepEqual(curvedPath[8], { time: 2, x: 80, y: 50 });
  assert.equal(motion.motionFrameNumberAtTime(1 / 30), 1);
  assert.equal(motion.snapMotionTimeToFrame(0.034, 2), 1 / 30);
  const retimed = motion.retimeMotionKeyframe(normalized, "end", 0.034, 3);
  assert.equal(retimed[1].time, 1 / 30);
  const collisionRetimed = motion.retimeMotionKeyframe(normalized, "end", 0.001, 3);
  assert.equal(collisionRetimed.length, 1);
  assert.equal(collisionRetimed[0].id, "end");
  const upserted = motion.upsertMotionKeyframe(normalized, {
    id: "replacement",
    time: 2.003,
    x: 80,
    y: 40,
    scale: 1,
    rotation: 0,
    opacity: 1,
    easing: "easeOut",
  });
  assert.equal(upserted.length, 2);
  assert.equal(upserted[1].id, "end");
  assert.equal(upserted[1].x, 80);
  const slidePreset = motion.createMotionPresetKeyframes("slideUp", 2, fallback, "slide");
  assert.equal(slidePreset.length, 2);
  assert.equal(slidePreset[0].id, "slide-start");
  assert.equal(slidePreset[0].y, 92);
  assert.equal(slidePreset[0].opacity, 0);
  assert.deepEqual(motion.motionPoseAtTime(slidePreset, 2, fallback), fallback);
  const popPreset = motion.createMotionPresetKeyframes("pop", 0.3, fallback, "pop");
  assert.equal(popPreset.length, 3);
  assert.equal(popPreset[2].time <= 0.3, true);
  assert.equal(popPreset[2].easing, "spring");
  const cinematicPreset = motion.createMotionPresetKeyframes("kenBurns", 4, fallback, "cinematic");
  assert.equal(cinematicPreset.length, 2);
  assert.equal(cinematicPreset[1].time, 4);
  assert.equal(cinematicPreset[1].easing, "gentle");
  assert.equal(cinematicPreset[1].scale, 1.08);
  const axisFrames = motion.normalizeMotionKeyframes([
    { ...fallback, id: "axis-start", time: 0, scaleX: 0.02, scaleY: 1, easing: "linear" },
    { ...fallback, id: "axis-end", time: 1, scaleX: 1, scaleY: 2, easing: "linear" },
  ], fallback, 1);
  const axisPose = motion.motionPoseAtTime(axisFrames, 0.5, fallback);
  assert.equal(axisPose.scaleX, 0.51);
  assert.equal(axisPose.scaleY, 1.5);
  const cinematicPropertyFrames = motion.normalizeMotionKeyframes([
    { ...fallback, id: "cinematic-start", time: 0, rotationX: 0, rotationY: -20, perspective: 900, blur: 18, effectStrength: 0.2, easing: "linear" },
    { ...fallback, id: "cinematic-end", time: 1, rotationX: 30, rotationY: 20, perspective: 1500, blur: 0, effectStrength: 0.8, easing: "linear" },
  ], fallback, 1);
  const cinematicPropertyPose = motion.motionPoseAtTime(cinematicPropertyFrames, 0.5, fallback);
  assert.equal(cinematicPropertyPose.rotationX, 15);
  assert.equal(cinematicPropertyPose.rotationY, 0);
  assert.equal(cinematicPropertyPose.perspective, 1200);
  assert.equal(cinematicPropertyPose.blur, 9);
  assert.equal(Math.abs(cinematicPropertyPose.effectStrength - 0.5) < 1e-9, true);
  const pathDrawFrames = motion.normalizeMotionKeyframes([
    { ...fallback, id: "path-hidden", time: 0, pathProgress: 0, easing: "linear" },
    { ...fallback, id: "path-drawn", time: 1, pathProgress: 1, easing: "linear" },
  ], fallback, 1);
  assert.equal(motion.motionPoseAtTime(pathDrawFrames, 0.25, fallback).pathProgress, 0.25);
  assert.equal(motion.normalizeMotionKeyframes([
    { ...fallback, id: "path-clamped", time: 0, pathProgress: 4, easing: "linear" },
  ], fallback, 1)[0].pathProgress, 1);
  assert.equal(timeline.normalizeVideoClipSpeed(undefined), 1);
  assert.equal(timeline.normalizeVideoClipSpeed(0.1), 0.25);
  assert.equal(timeline.normalizeVideoClipSpeed(8), 4);
  assert.equal(timeline.videoClipTimelineDuration(2, 10, 2), 4);
  assert.equal(timeline.videoClipTimelineDuration(2, 10, 0.5), 16);
  assert.equal(timeline.videoClipSourceTimeAtOffset(2, 10, 2, 3), 8);
  assert.equal(timeline.videoClipSourceTimeAtOffset(2, 10, 2, 8), 10);
  assert.equal(timeline.videoClipTimelineOffsetAtSourceTime(2, 10, 2, 8), 3);
  assert.deepEqual(timeline.videoOverlaySourceWindow(10, 2, 6, 4), { start: 2, end: 6, duration: 4 });
  assert.deepEqual(timeline.videoOverlaySourceWindow(null, null, null, 4), { start: 0, end: 4, duration: 4 });
  assert.equal(timeline.videoOverlaySourceTimeAtTimelineTime({ timelineStart: 3, timelineTime: 4, speed: 2, loop: false, assetDuration: 10, sourceStart: 2, sourceEnd: 6, fallbackDuration: 4 }), 4);
  assert.equal(timeline.videoOverlaySourceTimeAtTimelineTime({ timelineStart: 3, timelineTime: 6, speed: 2, loop: true, assetDuration: 10, sourceStart: 2, sourceEnd: 6, fallbackDuration: 4 }), 4);
  assert.equal(timeline.videoOverlaySourceTimeAtTimelineTime({ timelineStart: 3, timelineTime: 9, speed: 1, loop: false, assetDuration: 10, sourceStart: 2, sourceEnd: 6, fallbackDuration: 4 }), 5.999);
  assert.equal(timeline.normalizeVideoClipFade(9, 6), 3);
  assert.equal(timeline.normalizeVideoClipFade(-1, 6), 0);
  assert.equal(timeline.videoClipEdgeFadeOpacity(0, 6, 1, 1), 0);
  assert.equal(timeline.videoClipEdgeFadeOpacity(0.5, 6, 1, 1), 0.5);
  assert.equal(timeline.videoClipEdgeFadeOpacity(3, 6, 1, 1), 1);
  assert.equal(timeline.videoClipEdgeFadeOpacity(5.75, 6, 1, 1), 0.25);
  assert.equal(timeline.videoClipEdgeFadeOpacity(6, 6, 1, 1), 0);
  assert.deepEqual(motionDesign.MOTION_DESIGN_PRESETS, [
    "kinetic-type",
    "swiss-grid",
    "product-promo",
    "editorial-data",
    "product-film",
    "feature-reveal",
    "product-story",
    "motion-design",
    "texture-launch",
  ]);
  for (const preset of motionDesign.MOTION_DESIGN_PRESETS) {
    const composition = motionDesign.createMotionDesignComposition({
      preset,
      headline: `Generated ${preset}`,
      kicker: "MANOR AI",
      subhead: "Editable deterministic motion graphics",
      cta: "Create now",
      statValue: "+84%",
      statLabel: "MOMENTUM",
    }, 15);
    assert.equal(composition.preset, preset);
    assert.equal(composition.captions.length >= 5, true);
    assert.equal(composition.graphics.length >= 8, true);
    assert.equal(composition.shots.length, 4);
    assert.equal(composition.markers.length, 4);
    assert.equal(composition.graphics[0].width, 100);
    assert.equal(composition.graphics[0].height, 100);
    const allIds = [
      ...composition.captions.map((item) => item.id),
      ...composition.graphics.map((item) => item.id),
      ...composition.audioCues.map((item) => item.id),
      ...composition.shots.map((item) => item.id),
      ...composition.markers.map((item) => item.id),
    ];
    assert.equal(new Set(allIds).size, allIds.length);
    assert.equal(composition.captions.every((item) => item.end <= 15), true);
    assert.equal(composition.graphics.every((item) => item.end <= 15), true);
  }
  const darkSurfacePromo = motionDesign.createMotionDesignComposition({
    preset: "product-promo",
    surface: "#101225",
    accent: "#a259ff",
    accent2: "#0acf83",
  }, 15);
  assert.equal(darkSurfacePromo.captions.find((item) => item.id.endsWith("window-title"))?.color, "#f8fafc");
  assert.equal(darkSurfacePromo.captions.find((item) => item.id.endsWith("window-stat"))?.color, "#f8fafc");
  assert.equal(darkSurfacePromo.captions.find((item) => item.id.endsWith("window-label"))?.color, "#11131a");
  const productFilm = motionDesign.createMotionDesignComposition({ preset: "product-film" }, 15);
  assert.equal(productFilm.graphics.find((item) => item.id.endsWith("background"))?.opacity < 0.5, true);
  assert.equal(productFilm.graphics.find((item) => item.id.endsWith("background"))?.fillType, "linear");
  assert.equal(productFilm.graphics.some((item) => item.id.endsWith("code-panel")), true);
  assert.equal(productFilm.captions.some((item) => item.id.endsWith("subtitle")), true);
  const productFilmPanel = productFilm.graphics.find((item) => item.id.endsWith("code-panel"));
  assert.equal(productFilmPanel?.shadowBlur > 0, true);
  assert.equal(productFilmPanel?.keyframes.some((frame) => frame.id.endsWith("-hold") && frame.opacity === productFilmPanel.opacity), true);
  const featureReveal = motionDesign.createMotionDesignComposition({
    preset: "feature-reveal",
    quality: "production",
    visualTone: "technical",
    fontFamily: "display",
  }, 15);
  assert.equal(featureReveal.graphics.filter((item) => item.id.includes("bar-")).length, 4);
  assert.equal(featureReveal.captions.some((item) => item.text === "Discover"), true);
  assert.equal(featureReveal.captions.some((item) => item.text === "Automate"), true);
  assert.equal(featureReveal.graphics.some((item) => item.fillType === "linear"), true);
  assert.equal(featureReveal.graphics.some((item) => item.kind === "path"), true);
  assert.equal(featureReveal.graphics.some((item) => item.effect === "glass"), true);
  assert.equal(featureReveal.graphics.some((item) => item.effect === "grain"), true);
  assert.equal(featureReveal.graphics.filter((item) => item.id.includes("particle-")).length, 8);
  assert.equal(featureReveal.graphics.find((item) => item.id.endsWith("surface-sheen"))?.keyframes.length, 4);
  assert.equal(featureReveal.graphics.find((item) => item.id.endsWith("bar-automate"))?.keyframes.some((frame) => frame.scaleX === 0.02), true);
  assert.equal(featureReveal.captions.find((item) => item.id.endsWith("kicker"))?.fontFamily, "mono");
  assert.equal(featureReveal.captions.find((item) => item.id.endsWith("headline"))?.fontFamily, "display");
  assert.equal(featureReveal.captions.find((item) => item.id.endsWith("headline"))?.reveal, "words");
  const productStory = motionDesign.createMotionDesignComposition({ preset: "product-story", quality: "production" }, 15);
  assert.equal(productStory.graphics.filter((item) => item.id.endsWith("window")).length, 3);
  assert.equal(productStory.graphics.some((item) => item.id.endsWith("browser-bar")), true);
  assert.equal(productStory.graphics.find((item) => item.id.endsWith("front-window"))?.shadowBlur > 0, true);
  assert.equal(productStory.graphics.find((item) => item.id.endsWith("cursor"))?.kind, "path");
  assert.equal(productStory.graphics.find((item) => item.id.endsWith("cursor"))?.keyframes.length >= 5, true);
  assert.equal(productStory.graphics.filter((item) => item.id.includes("click-ripple-")).length, 2);
  const motionDesignFilm = motionDesign.createMotionDesignComposition({ preset: "motion-design", quality: "production" }, 15);
  assert.equal(motionDesignFilm.graphics.filter((item) => item.id.includes("ambient-")).length, 2);
  assert.equal(motionDesignFilm.graphics.filter((item) => item.id.includes("cinema-matte-")).length, 2);
  assert.equal(motionDesignFilm.graphics.find((item) => item.id.endsWith("hook-to-editor-light-leak"))?.effect, "lightLeak");
  assert.equal(motionDesignFilm.graphics.find((item) => item.id.endsWith("editor-rack-focus"))?.effect, "halation");
  assert.equal(motionDesignFilm.graphics.find((item) => item.id.endsWith("proof-to-resolve-film-burn"))?.effect, "filmBurn");
  assert.equal(motionDesignFilm.graphics.find((item) => item.id.endsWith("editor-anamorphic-flare"))?.effect, "anamorphic");
  assert.equal(motionDesignFilm.captions.some((item) => item.id.endsWith("headline")), true);
  assert.equal(motionDesignFilm.graphics.filter((item) => item.id.includes("editor-depth-")).length, 2);
  assert.equal(motionDesignFilm.graphics.find((item) => item.id.endsWith("editor-shell"))?.effect, "glass");
  assert.equal(motionDesignFilm.graphics.filter((item) => item.id.includes("canvas-code-line-")).length, 3);
  assert.equal(motionDesignFilm.graphics.find((item) => item.id.endsWith("editor-cursor"))?.keyframes.length, 5);
  assert.equal(motionDesignFilm.graphics.filter((item) => item.id.includes("click-ripple-")).length, 2);
  assert.equal(motionDesignFilm.graphics.filter((item) => item.id.includes("proof-bar-")).length, 5);
  assert.equal(motionDesignFilm.graphics.find((item) => item.id.endsWith("proof-trend"))?.kind, "path");
  assert.equal(motionDesignFilm.graphics.some((item) => item.fillType === "radial"), true);
  assert.equal(motionDesignFilm.graphics.filter((item) => item.id.includes("particle-") && item.kind !== "particle").length, 14);
  assert.equal(motionDesignFilm.graphics.find((item) => item.id.endsWith("deterministic-particle-atmosphere"))?.kind, "particle");
  assert.equal(motionDesignFilm.graphics.find((item) => item.id.endsWith("top-block"))?.maskShape, "hexagon");
  const textureLaunch = motionDesign.createMotionDesignComposition({ preset: "texture-launch", quality: "production" }, 22);
  assert.equal(textureLaunch.captions.some((item) => item.text === "PAPER"), true);
  assert.equal(textureLaunch.captions.some((item) => item.text.includes("MATERIAL")), true);
  assert.equal(textureLaunch.graphics.filter((item) => item.id.includes("look-") && item.id.endsWith("-background")).length, 7);
  assert.equal(textureLaunch.graphics.some((item) => item.id.includes("warp-ribbon")), true);
  assert.equal(textureLaunch.graphics.some((item) => item.kind === "particle" && item.particleMotion === "orbit"), true);
  assert.equal(textureLaunch.graphics.some((item) => item.kind === "shader" && item.shaderPreset === "domainWarp"), true);
  assert.equal(textureLaunch.graphics.some((item) => item.kind === "shader" && item.shaderPreset === "liquidMetal"), true);
  assert.equal(textureLaunch.graphics.some((item) => item.kind === "shader" && item.shaderPreset === "inkBloom"), true);
  assert.equal(textureLaunch.graphics.some((item) => item.kind === "shader" && item.shaderPreset === "filmBurn"), true);
  assert.equal(textureLaunch.graphics.some((item) => item.kind === "shader" && item.shaderPreset === "prismaticBurst"), true);
  assert.equal(textureLaunch.graphics.some((item) => item.kind === "shader" && item.shaderPreset === "parallaxGrid"), true);
  assert.equal(textureLaunch.audioCues.filter((item) => item.type === "sfx").length, 3);
  assert.equal(textureLaunch.audioCues.some((item) => item.type === "music" && item.loop), true);
  assert.deepEqual(shader.VIDEO_EDITOR_SHADER_PRESETS, [
    "domainWarp",
    "liquidMetal",
    "volumetricFog",
    "chromaticTunnel",
    "inkBloom",
    "filmBurn",
    "prismaticBurst",
    "parallaxGrid",
  ]);
  assert.deepEqual(shader.normalizeVideoEditorShaderStyle({
    shaderPreset: "liquidMetal",
    shaderSpeed: 99,
    shaderScale: 0,
    shaderBloom: -2,
    shaderGrain: 2,
  }), {
    shaderPreset: "liquidMetal",
    shaderSeed: 11,
    shaderSpeed: 4,
    shaderScale: 0.2,
    shaderIntensity: 1,
    shaderBloom: 0,
    shaderGrain: 0.35,
    shaderCameraX: 0,
    shaderCameraY: 0,
    shaderCameraZ: 1,
  });
  assert.equal(motionDesign.createMotionDesignComposition({ preset: "unknown" }, 15), null);
  assert.equal(visualStyle.revealedCaptionText("Build the system", "words", 0.2, 1), "Build");
  assert.equal(visualStyle.revealedCaptionText("MOVE", "characters", 0.5, 1), "MO");
  assert.equal(visualStyle.graphicCssFill("#111111", {
    fillType: "linear",
    fillSecondary: "#eeeeee",
    gradientAngle: 45,
  }), "linear-gradient(45deg, #111111, #eeeeee)");
  assert.equal(visualStyle.normalizeCaptionVisualStyle({ fontWeight: 955, maxWidth: 120 }).fontWeight, 900);
  assert.equal(visualStyle.normalizeCaptionVisualStyle({ fontWeight: 955, maxWidth: 120 }).maxWidth, 96);
  assert.equal(visualStyle.normalizeGraphicVisualStyle({ effect: "grain", effectStrength: 4 }).effect, "grain");
  assert.equal(visualStyle.normalizeGraphicVisualStyle({ effect: "grain", effectStrength: 4 }).effectStrength, 1);
  assert.equal(visualStyle.normalizeGraphicVisualStyle({ effect: "lightLeak" }).effect, "lightLeak");
  assert.equal(visualStyle.normalizeGraphicVisualStyle({ effect: "filmBurn" }).effect, "filmBurn");
  assert.equal(visualStyle.normalizeGraphicVisualStyle({ effect: "halation" }).effect, "halation");
  assert.equal(visualStyle.normalizeGraphicVisualStyle({ effect: "anamorphic" }).effect, "anamorphic");
  assert.match(visualStyle.graphicEffectOverlayBackground({ effect: "grain", effectStrength: 0.5 }), /repeating-radial-gradient/);
  assert.match(visualStyle.graphicEffectOverlayBackground({ effect: "lightLeak", effectStrength: 0.7 }), /radial-gradient/);
  assert.match(visualStyle.graphicEffectOverlayBackground({ effect: "filmBurn", effectStrength: 0.7 }), /rgba\(255,249,210/);
  assert.match(visualStyle.graphicEffectOverlayBackground({ effect: "anamorphic", effectStrength: 0.7 }), /linear-gradient/);
  assert.match(visualStyle.graphicCssClipPath("hexagon"), /polygon/);
  const particleConfig = particles.normalizeParticleLayer({
    particleCount: 100,
    particleShape: "spark",
    particleMotion: "burst",
    particleSeed: 42,
    particleLoop: true,
  });
  assert.equal(particleConfig.particleCount, 40);
  assert.equal(particles.particleFrameAtTime(particleConfig, 3, 0, 3, 640, 360).opacity, 0);
  assert.equal(particles.particleFrameAtTime(particleConfig, 3, 3, 3, 640, 360).opacity, 0);
  assert.deepEqual(
    particles.particleFrameAtTime(particleConfig, 7, 0.73, 3, 640, 360),
    particles.particleFrameAtTime(particleConfig, 7, 0.73, 3, 640, 360),
  );
  assert.notDeepEqual(
    particles.particleFrameAtTime(particleConfig, 7, 0.73, 3, 640, 360),
    particles.particleFrameAtTime({ ...particleConfig, particleSeed: 43 }, 7, 0.73, 3, 640, 360),
  );
  const editorSource = await readFile(path.join(root, "apps/web/src/pages/VideoEditor.tsx"), "utf8");
  const apiSource = await readFile(path.join(root, "apps/web/src/lib/api.ts"), "utf8");
  const toastSource = await readFile(path.join(root, "apps/web/src/components/ToastContainer.tsx"), "utf8");
  assert.match(editorSource, /api\.videoEditor\.finalizePreview\(/);
  assert.match(editorSource, /targetDurationSeconds: exportRangeDuration/);
  assert.match(editorSource, /Recover the missing tail deterministically/);
  assert.match(editorSource, /const recoveryStart = Math\.max\(sourceStart, activeVideo\.currentTime \+ frameStep\)/);
  assert.match(editorSource, /const shouldPlaySourceRealtime = trackStates\.video\.visible \|\| shouldCaptureAudio/);
  assert.match(editorSource, /if \(trackStates\.video\.visible\) await seekVideo\(activeVideo, sourceTime\)/);
  assert.match(editorSource, /Keep the recorder continuously active for code-only timelines/);
  assert.match(editorSource, /canvas\.captureStream\(useExplicitCanvasFrames \? 0 : 30\)/);
  assert.match(editorSource, /canvasVideoTrack\.requestFrame\?\.\(\)/);
  assert.match(editorSource, /-edited\.mp4/);
  assert.match(editorSource, /api\.videoEditor\.linkRecipe\(/);
  assert.match(editorSource, /activeCaptions\.map\(/);
  assert.match(editorSource, /addTextLayer\("titleCard"\)/);
  assert.match(editorSource, /addTextLayer\("lowerThird"\)/);
  assert.match(editorSource, /caption\.style !== "titleCard"/);
  assert.match(editorSource, /type GraphicLayerKind = "group" \| "rectangle" \| "ellipse" \| "line" \| "path" \| "particle" \| "shader" \| "image" \| "video"/);
  assert.match(editorSource, /graphics: graphicLayers/);
  assert.match(editorSource, /activeGraphicLayers\.map\(/);
  assert.match(editorSource, /graphicImageElementCache\.get\(/);
  assert.match(editorSource, /native_cinematic_effects_and_masks/);
  assert.match(editorSource, /kind === "group" \|\| kind === "shader" \? 100 : kind === "image" \|\| kind === "video" \? 32 : 24/);
  assert.match(editorSource, /kind === "group" \|\| kind === "shader" \? 100 : kind === "image" \|\| kind === "video" \? 32 : 18/);
  assert.match(editorSource, /drawCanvasGraphicEffect\(/);
  assert.match(editorSource, /style\.effect === "lightLeak"/);
  assert.match(editorSource, /style\.effect === "filmBurn"/);
  assert.match(editorSource, /style\.effect === "halation"/);
  assert.match(editorSource, /style\.effect === "anamorphic"/);
  assert.match(editorSource, /await Promise\.all\(graphicImageAssetIds\.map/);
  assert.match(editorSource, /beginCueDrag\(event, "graphic"/);
  assert.match(editorSource, /addGraphicLayer\("rectangle"\)/);
  assert.match(editorSource, /addGraphicLayer\("ellipse"\)/);
  assert.match(editorSource, /accept="image\/\*"/);
  assert.match(editorSource, /fit: "contain"/);
  assert.match(editorSource, /clipMotionPoseAtLocalTime/);
  assert.match(editorSource, /fittedMediaSize\(/);
  assert.match(editorSource, /activeVideoMap\?\.clip\.fit/);
  assert.match(editorSource, /applyClipMotionPreset/);
  assert.match(editorSource, /clipPreviewPlaybackRate/);
  assert.match(editorSource, /playback_rate: normalizeVideoClipSpeed/);
  assert.match(editorSource, /activeVideo\.playbackRate = normalizeVideoClipSpeed/);
  assert.match(editorSource, /activeClipPose\.opacity \* activeClipEdgeFadeOpacity/);
  assert.match(editorSource, /activeVideoMotionPose\.opacity \* activeVideoEdgeFadeOpacity/);
  assert.match(editorSource, /fade_in: clip\.fadeIn/);
  assert.match(editorSource, /MotionKeyframeControls/);
  assert.match(editorSource, /retimeMotionKeyframe/);
  assert.match(editorSource, /motionPathSamples/);
  assert.match(editorSource, /ve-motion-path-overlay/);
  assert.match(editorSource, /normalizeMotionBezier/);
  assert.match(editorSource, /live_edit_context:/);
  assert.match(editorSource, /playhead_frame: motionFrameNumberAtTime\(playhead\)/);
  assert.match(editorSource, /custom_cubic_bezier/);
  assert.match(editorSource, /smooth_spatial_paths/);
  assert.match(editorSource, /native_motion_design_generation/);
  assert.match(editorSource, /depth_tilt_and_perspective/);
  assert.match(editorSource, /keyframed_blur_and_effect_strength/);
  assert.match(editorSource, /source_trimmed_audio_waveforms/);
  assert.match(editorSource, /multi_video_overlay_tracks/);
  assert.match(editorSource, /deterministic_particle_layers/);
  assert.match(editorSource, /drawParticleLayer\(/);
  assert.match(editorSource, /addGraphicLayer\("particle"\)/);
  assert.match(editorSource, /addGraphicLayer\("shader"\)/);
  assert.match(editorSource, /renderVideoEditorShaderFrame\(/);
  assert.match(editorSource, /handleGraphicVideoUpload/);
  assert.match(editorSource, /getGraphicVideoSourceTime/);
  assert.match(editorSource, /syncGraphicVideoRenderers/);
  assert.match(editorSource, /scene_parent_group_keyframes/);
  assert.match(editorSource, /nested_layer_groups/);
  assert.match(editorSource, /clip_containers/);
  assert.match(editorSource, /reusable_subscene_instances/);
  assert.match(editorSource, /keyframed_path_drawing/);
  assert.match(editorSource, /kind === "group"/);
  assert.match(editorSource, /clipChildren/);
  assert.match(editorSource, /instanceOf/);
  assert.match(editorSource, /pathProgress/);
  assert.match(editorSource, /drawCanvasPartialSvgPath/);
  assert.match(editorSource, /renderGraphicOverlay/);
  assert.match(editorSource, /function composeMotionPoses/);
  assert.match(editorSource, /Scene group motion/);
  assert.match(editorSource, /function TimelineAudioWaveform/);
  assert.match(editorSource, /getAudioCueSourceWindow/);
  assert.match(editorSource, /const shouldCaptureAudio = !trackStates\.audio\.muted/);
  assert.match(editorSource, /else if \(shouldCaptureAudio\)/);
  assert.match(editorSource, /production_typography/);
  assert.match(editorSource, /gradient_and_vector_layers/);
  assert.match(editorSource, /canvasGraphicFill/);
  assert.match(editorSource, /revealedCaptionText/);
  assert.match(editorSource, /createMotionDesignComposition\(recipe\.motion_design/);
  assert.match(editorSource, /motion_design_presets: MOTION_DESIGN_PRESETS/);
  assert.match(editorSource, /getContent: \(\) => editorLiveContentRef\.current/);
  assert.match(editorSource, /ai\.example\.camera_move/);
  assert.match(editorSource, /version: 11/);
  assert.match(editorSource, /if \(routeIsRecipe \|\| recipeDoc\) return/);
  assert.match(apiSource, /video-editor\/link-recipe/);
  assert.match(apiSource, /form\.append\("target_duration_seconds"/);
  assert.match(apiSource, /\/media\/free-music\/search/);
  assert.match(apiSource, /\/media\/free-music\/import/);
  assert.match(editorSource, /api\.videoEditor\.searchFreeMusic/);
  assert.match(editorSource, /api\.videoEditor\.importFreeMusic/);
  assert.match(editorSource, /musicAttribution/);
  assert.match(editorSource, /CC0 · CC BY/);
  assert.match(editorSource, /key: "technology", query: "technology"/);
  assert.match(editorSource, /fallbackQuery = submittedFreeMusicSearch\.split/);
  assert.match(editorSource, /freeMusicPreviewProgress/);
  assert.match(editorSource, /ve-free-music-preview-ring/);
  assert.match(editorSource, /ve-free-music-meter/);
  assert.match(editorSource, /addedFreeMusicIds/);
  assert.match(editorSource, /free_music\.added/);
  assert.match(toastSource, /zIndex: 20050/);
  console.log("video editor geometry, motion, timeline, and graphics tests passed");
} finally {
  await rm(temporaryDirectory, { recursive: true, force: true });
}
