/**
 * Hero avatar - the Vaani particle portrait, driven by scroll.
 *
 * Marketing-page cousin of the interactive avatar in index.html: same
 * image-sampled point cloud, but no microphone, no TTS analyser and no
 * lip-sync, so the landing page needs no permissions.
 *
 * All per-particle motion (drift, breathing, dispersion) runs in the vertex
 * shader. Animating 46k points on the CPU each frame is what makes this kind
 * of hero stutter during scroll; here JS only writes a handful of uniforms.
 *
 * Scroll choreography is fed in via setProgress(0..1) and damped internally,
 * so even coarse scroll events produce continuous motion.
 */

import * as THREE from './three.module.js';

const CONFIG = {
  particleCount: 46000,
  sampleThreshold: 26,   // luminance floor - skips the dark background
  spread: 4.6,           // world width the portrait occupies
  depthAmount: 0.55,     // brighter pixels sit closer to camera
  particleSize: 9.5,     // in px at reference distance
  color: new THREE.Color(0xd8d0ff),
  colorDark: new THREE.Color(0x5b4f92),
  cameraNear: 3.05,      // fully zoomed in (end of scroll)
  cameraFar: 6.2,        // wide shot (top of page)
};

const VERT = /* glsl */`
  attribute vec3  aBase;
  attribute float aRandom;
  attribute vec2  aSeed;

  uniform float uTime;
  uniform float uProgress;   // 0 = top of page, 1 = end of hero sequence
  uniform float uDisperse;   // extra scatter, drives the dissolve
  uniform float uSize;
  uniform float uPixelRatio;

  varying float vFade;

  void main() {
    vec3 p = aBase;

    // Slow breathing swell.
    p *= 1.0 + sin(uTime * 0.55) * 0.006;

    // Per-particle drift so the cloud never looks frozen.
    float drift = 0.012 + aRandom * 0.016;
    p.x += sin(uTime * 0.50 + aSeed.x) * drift;
    p.y += cos(uTime * 0.42 + aSeed.y) * drift;
    p.z += sin(uTime * 0.70 + aSeed.x * 0.5) * drift * 0.8;

    // Dissolve: particles fly outward along their own direction as the
    // hero hands over to the page content. Randomised so it frays.
    float scatter = uDisperse * (0.35 + aRandom * 1.65);
    p += normalize(p + vec3(0.001)) * scatter;
    p.y += uDisperse * (aRandom - 0.5) * 1.2;

    vec4 mv = modelViewMatrix * vec4(p, 1.0);
    gl_Position = projectionMatrix * mv;

    // Perspective-correct point size.
    gl_PointSize = uSize * uPixelRatio * (1.0 / -mv.z);

    // Fade the scattered particles out so the dissolve reads as a fade,
    // and dim slightly as we zoom so the face does not blow out.
    vFade = (1.0 - uDisperse * 0.85) * (1.0 - uProgress * 0.15);
  }
`;

const FRAG = /* glsl */`
  varying float vFade;
  uniform vec3 uColorA;

  void main() {
    // Soft round sprite - cheaper and crisper than a texture lookup.
    vec2 d = gl_PointCoord - vec2(0.5);
    float r = dot(d, d);
    if (r > 0.25) discard;
    float alpha = smoothstep(0.25, 0.0, r) * vFade;
    gl_FragColor = vec4(uColorA, alpha * 0.92);
  }
`;

export function createHeroAvatar({ canvas, imageSrc, onReady } = {}) {
  const renderer = new THREE.WebGLRenderer({
    canvas,
    antialias: true,
    alpha: true,
    powerPreference: 'high-performance',
  });
  const pixelRatio = Math.min(devicePixelRatio, 2);
  renderer.setPixelRatio(pixelRatio);
  renderer.outputColorSpace = THREE.SRGBColorSpace;

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(38, 1, 0.1, 100);
  camera.position.set(0, 0, CONFIG.cameraFar);

  const uniforms = {
    uTime: { value: 0 },
    uProgress: { value: 0 },
    uDisperse: { value: 0 },
    uSize: { value: CONFIG.particleSize },
    uPixelRatio: { value: pixelRatio },
    uColorA: { value: CONFIG.color.clone() },
  };

  let points = null;
  let disposed = false;
  let ready = false;

  // Damped values: targets are set from scroll/pointer, actuals chase them.
  let progressTarget = 0;
  let progress = 0;
  const pointer = new THREE.Vector2();
  const pointerTarget = new THREE.Vector2();

  function build(img) {
    const iw = img.naturalWidth || 480;
    const ih = img.naturalHeight || 480;
    const scale = Math.min(1, 480 / iw);
    const cw = Math.round(iw * scale);
    const ch = Math.round(ih * scale);

    const c = document.createElement('canvas');
    c.width = cw;
    c.height = ch;
    const cx = c.getContext('2d', { willReadFrequently: true });
    cx.drawImage(img, 0, 0, cw, ch);
    const px = cx.getImageData(0, 0, cw, ch).data;

    const cand = [];
    for (let y = 0; y < ch; y++) {
      for (let x = 0; x < cw; x++) {
        const i = (y * cw + x) * 4;
        const l = 0.2126 * px[i] + 0.7152 * px[i + 1] + 0.0722 * px[i + 2];
        if (l > CONFIG.sampleThreshold) cand.push(x, y, l);
      }
    }

    const nC = cand.length / 3;
    if (!nC) return;
    const count = Math.min(CONFIG.particleCount, nC * 4);

    const base = new Float32Array(count * 3);
    const colors = new Float32Array(count * 3);
    const randoms = new Float32Array(count);
    const seeds = new Float32Array(count * 2);

    // Importance-sample by luminance so highlights get more particles.
    const weights = new Float32Array(nC);
    let total = 0;
    for (let k = 0; k < nC; k++) {
      total += Math.pow(cand[k * 3 + 2] / 255, 0.85);
      weights[k] = total;
    }

    const aspect = cw / ch;
    const worldW = CONFIG.spread;
    const worldH = CONFIG.spread / aspect;
    const col = new THREE.Color();

    for (let i = 0; i < count; i++) {
      const t = Math.random() * total;
      let lo = 0;
      let hi = nC - 1;
      while (lo < hi) {
        const m = (lo + hi) >> 1;
        if (weights[m] < t) lo = m + 1;
        else hi = m;
      }
      const sx = cand[lo * 3];
      const sy = cand[lo * 3 + 1];
      const l = cand[lo * 3 + 2] / 255;

      const j = i * 3;
      base[j] = ((sx + Math.random() - 0.5) / cw - 0.5) * worldW;
      base[j + 1] = -((sy + Math.random() - 0.5) / ch - 0.5) * worldH;
      base[j + 2] = (l - 0.35) * CONFIG.depthAmount + (Math.random() - 0.5) * 0.05;

      randoms[i] = Math.random();
      seeds[i * 2] = Math.random() * 100;
      seeds[i * 2 + 1] = Math.random() * 100;

      col.copy(CONFIG.colorDark).lerp(CONFIG.color, Math.min(1, l * 0.9));
      const b = 0.72 + Math.random() * 0.28;
      colors[j] = col.r * b;
      colors[j + 1] = col.g * b;
      colors[j + 2] = col.b * b;
    }

    const geo = new THREE.BufferGeometry();
    // `position` is required by three even though the shader reads aBase.
    geo.setAttribute('position', new THREE.BufferAttribute(base, 3));
    geo.setAttribute('aBase', new THREE.BufferAttribute(base, 3));
    geo.setAttribute('color', new THREE.BufferAttribute(colors, 3));
    geo.setAttribute('aRandom', new THREE.BufferAttribute(randoms, 1));
    geo.setAttribute('aSeed', new THREE.BufferAttribute(seeds, 2));
    geo.computeBoundingSphere();

    const mat = new THREE.ShaderMaterial({
      uniforms,
      vertexShader: VERT,
      fragmentShader: FRAG,
      transparent: true,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
    });

    points = new THREE.Points(geo, mat);
    // The cloud is never culled: dispersion pushes points past the bounds.
    points.frustumCulled = false;
    scene.add(points);

    ready = true;
    if (onReady) onReady();
  }

  function resize() {
    const w = canvas.clientWidth || innerWidth;
    const h = canvas.clientHeight || innerHeight;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  }

  const clock = new THREE.Clock();
  const lerp = (a, b, t) => a + (b - a) * t;

  function frame() {
    if (disposed) return;
    requestAnimationFrame(frame);
    if (!ready) return;

    // Damping is what makes coarse scroll events feel continuous.
    progress = lerp(progress, progressTarget, 0.075);
    pointer.lerp(pointerTarget, 0.045);

    uniforms.uTime.value = clock.getElapsedTime();
    uniforms.uProgress.value = progress;

    // Dissolve only in the last third of the sequence.
    uniforms.uDisperse.value = Math.pow(Math.max(0, (progress - 0.62) / 0.38), 1.7) * 1.5;

    // Dolly in as the visitor scrolls, and drift the face slightly upward
    // so the headline below it has room.
    camera.position.z = lerp(CONFIG.cameraFar, CONFIG.cameraNear, progress);
    camera.position.y = progress * 0.22;

    points.rotation.y = pointer.x * 0.16 + progress * 0.05;
    points.rotation.x = -pointer.y * 0.10;

    renderer.render(scene, camera);
  }

  function onPointerMove(e) {
    pointerTarget.x = (e.clientX / innerWidth) * 2 - 1;
    pointerTarget.y = (e.clientY / innerHeight) * 2 - 1;
  }

  const img = new Image();
  img.onload = () => build(img);
  img.onerror = () => console.warn('Hero avatar: portrait failed to load', imageSrc);
  img.src = imageSrc;

  resize();
  addEventListener('resize', resize);
  addEventListener('pointermove', onPointerMove, { passive: true });
  frame();

  return {
    /** Drive the choreography. p is clamped 0..1. */
    setProgress(p) {
      progressTarget = Math.min(1, Math.max(0, p));
    },
    dispose() {
      disposed = true;
      removeEventListener('resize', resize);
      removeEventListener('pointermove', onPointerMove);
      points?.geometry.dispose();
      points?.material.dispose();
      renderer.dispose();
    },
  };
}
