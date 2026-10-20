import { useEffect, useRef, useState } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { CSS2DObject, CSS2DRenderer } from "three/examples/jsm/renderers/CSS2DRenderer.js";
import { fmtSci } from "../../format";
import type { Display } from "../../types";

type ContourDisplay = Extract<Display, { kind: "contour" }>;

const NAN_COLOR = [0.62, 0.64, 0.68];

/** Abaqus' spectra, low to high, in discrete bands. Rainbow: blue → cyan → green → yellow → red. */
export function bandColors(n: number, spectrum = "Rainbow"): number[][] {
  return Array.from({ length: n }, (_, i) => {
    const t = i / Math.max(n - 1, 1);
    if (spectrum === "Black to white" || spectrum === "White to black") {
      const g = spectrum === "Black to white" ? 0.08 + 0.9 * t : 0.98 - 0.9 * t;
      return [g, g, g];
    }
    if (spectrum === "Blue to red") {  // diverging: blue → white → red
      const c = t < 0.5 ? new THREE.Color(0x2b59c3).lerp(new THREE.Color(0xf2f2f2), t * 2)
        : new THREE.Color(0xf2f2f2).lerp(new THREE.Color(0xd7301f), (t - 0.5) * 2);
      return [c.r, c.g, c.b];
    }
    const u = spectrum === "Reversed rainbow" ? 1 - t : t;
    const c = new THREE.Color().setHSL((240 * (1 - u)) / 360, 1, 0.5);
    return [c.r, c.g, c.b];
  });
}

function makeTexture(n: number, spectrum?: string) {
  // Texel 0 is the "no value" grey; texels 1..n are the bands. Nearest filtering keeps the bands crisp.
  const data = new Uint8Array((n + 1) * 4);
  [NAN_COLOR, ...bandColors(n, spectrum)].forEach((c, i) => {
    data.set([c[0] * 255, c[1] * 255, c[2] * 255, 255], i * 4);
  });
  const tex = new THREE.DataTexture(data, n + 1, 1, THREE.RGBAFormat);
  tex.magFilter = THREE.NearestFilter;
  tex.minFilter = THREE.NearestFilter;
  tex.colorSpace = THREE.SRGBColorSpace;
  tex.needsUpdate = true;
  return tex;
}

interface Scene {
  renderer: THREE.WebGLRenderer;
  scene: THREE.Scene;
  camera: THREE.PerspectiveCamera;
  controls: OrbitControls;
  group: THREE.Group;
  markers: THREE.Group;
  labels: CSS2DRenderer;
  mesh?: THREE.Mesh;
  fitted: boolean;
  view?: string;
}

export function ContourView({ display }: { display: ContourDisplay }) {
  const host = useRef<HTMLDivElement>(null);
  const sceneRef = useRef<Scene | null>(null);
  const [probe, setProbe] = useState<{ x: number; y: number; v: number | null } | null>(null);
  const n = Math.max(2, Math.min(24, display.bands || 12));

  // One renderer for the lifetime of the view.
  useEffect(() => {
    const el = host.current!;
    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, preserveDrawingBuffer: true });
    renderer.setPixelRatio(window.devicePixelRatio);
    el.appendChild(renderer.domElement);
    const scene = new THREE.Scene();
    scene.add(new THREE.AmbientLight(0xffffff, 1.5));
    const sun = new THREE.DirectionalLight(0xffffff, 1.6);
    sun.position.set(1, 2, 3);
    const camera = new THREE.PerspectiveCamera(35, 1, 0.01, 1e7);
    camera.add(sun);
    scene.add(camera);
    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    const group = new THREE.Group();
    scene.add(group);
    const markers = new THREE.Group();
    scene.add(markers);
    const labels = new CSS2DRenderer();
    labels.domElement.className = "contour-labels";
    el.appendChild(labels.domElement);
    const s: Scene = { renderer, scene, camera, controls, group, markers, labels, fitted: false };
    sceneRef.current = s;

    const resize = () => {
      const w = el.clientWidth, h = el.clientHeight;
      renderer.setSize(w, h);
      labels.setSize(w, h);
      camera.aspect = w / Math.max(h, 1);
      camera.updateProjectionMatrix();
    };
    const ro = new ResizeObserver(resize);
    ro.observe(el);
    resize();
    let frame = 0;
    const loop = () => {
      frame = requestAnimationFrame(loop);
      controls.update();
      renderer.render(scene, camera);
      labels.render(scene, camera);
    };
    loop();
    return () => {
      cancelAnimationFrame(frame);
      ro.disconnect();
      controls.dispose();
      group.traverse((o) => {
        const m = o as THREE.Mesh;
        m.geometry?.dispose();
        (m.material as THREE.Material | undefined)?.dispose?.();
      });
      markers.clear();
      renderer.dispose();
      el.removeChild(renderer.domElement);
      el.removeChild(labels.domElement);
      sceneRef.current = null;
    };
  }, []);

  // Rebuild geometry when the data changes, keeping the camera where the user left it.
  useEffect(() => {
    const s = sceneRef.current;
    if (!s) return;
    s.group.children.forEach((o) => {
      const m = o as THREE.Mesh;
      m.geometry.dispose();
      const mat = m.material as THREE.MeshPhongMaterial;
      mat.map?.dispose();
      mat.dispose();
    });
    s.group.clear();

    const span = display.max - display.min || 1;
    const uv = new Float32Array(display.values.length * 2);
    display.values.forEach((v, i) => {
      const t = v === null ? -1 : Math.min(Math.max((v - display.min) / span, 0), 1);
      uv[i * 2] = t < 0 ? 0.5 / (n + 1) : (1 + Math.min(t * n, n - 1e-3)) / (n + 1);
      uv[i * 2 + 1] = 0.5;
    });
    const geom = new THREE.BufferGeometry();
    geom.setAttribute("position", new THREE.Float32BufferAttribute(display.positions, 3));
    geom.setAttribute("uv", new THREE.BufferAttribute(uv, 2));
    geom.setIndex(display.triangles);
    geom.computeVertexNormals();
    const mesh = new THREE.Mesh(
      geom,
      new THREE.MeshPhongMaterial({
        map: makeTexture(n, display.spectrum), side: THREE.DoubleSide, flatShading: true, shininess: 0,
        polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1,
      }),
    );
    s.group.add(mesh);
    s.mesh = mesh;

    if (display.lines.length) {
      const lg = new THREE.BufferGeometry();
      lg.setAttribute("position", new THREE.Float32BufferAttribute(display.positions, 3));
      lg.setIndex(display.lines);
      s.group.add(new THREE.LineSegments(lg, new THREE.LineBasicMaterial({ color: 0x000000, transparent: true, opacity: 0.45 })));
    }
    // Results from Reduce nodes: a dot where the value occurs, with a label.
    s.markers.children.forEach((o) => {
      const m = o as THREE.Mesh;
      m.geometry?.dispose();
      (m.material as THREE.Material | undefined)?.dispose?.();
    });
    s.markers.clear();
    const many = display.markers.length > 30;
    const radius = (new THREE.Box3().setFromObject(s.group).getSize(new THREE.Vector3()).length() || 1) * (many ? 0.004 : 0.008);
    display.markers.forEach((mk) => {
      const dot = new THREE.Mesh(
        new THREE.SphereGeometry(radius, 16, 12),
        new THREE.MeshBasicMaterial({ color: 0xffffff, depthTest: false }),
      );
      dot.renderOrder = 10;
      dot.position.set(...mk.position);
      s.markers.add(dot);
      const ring = new THREE.Mesh(
        new THREE.SphereGeometry(radius * 1.6, 16, 12),
        new THREE.MeshBasicMaterial({ color: 0x111111, depthTest: false, transparent: true, opacity: 0.6 }),
      );
      ring.renderOrder = 9;
      ring.position.copy(dot.position);
      s.markers.add(ring);
      if (!mk.text) return; // many markers: dots only
      const div = document.createElement("div");
      div.className = "marker-label";
      div.textContent = mk.text;
      div.title = mk.where;
      const label = new CSS2DObject(div);
      label.position.copy(dot.position);
      label.center.set(-0.08, 1.4);
      s.markers.add(label);
    });

    // Fit once, and again when the chosen orientation changes (otherwise keep the user's camera).
    if (!s.fitted || s.view !== display.view) {
      fit(s, display.view);
      s.fitted = true;
      s.view = display.view;
    }
  }, [display, n]);

  const onMove = (e: React.MouseEvent) => {
    const s = sceneRef.current;
    if (!s?.mesh || e.buttons) {
      setProbe(null);
      return;
    }
    const r = (e.currentTarget as HTMLElement).getBoundingClientRect();
    const ndc = new THREE.Vector2(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
    const ray = new THREE.Raycaster();
    ray.setFromCamera(ndc, s.camera);
    const hit = ray.intersectObject(s.mesh)[0];
    if (!hit?.face) {
      setProbe(null);
      return;
    }
    const pos = s.mesh.geometry.getAttribute("position");
    const p = new THREE.Vector3();
    let best = hit.face.a, dist = Infinity;
    for (const i of [hit.face.a, hit.face.b, hit.face.c]) {
      const d = p.fromBufferAttribute(pos, i).distanceTo(hit.point);
      if (d < dist) {
        dist = d;
        best = i;
      }
    }
    setProbe({ x: e.clientX - r.left, y: e.clientY - r.top, v: display.values[best] });
  };

  const colors = bandColors(n, display.spectrum);
  const span = display.max - display.min;
  return (
    <div className="contour">
      <div className="contour-canvas" ref={host} onMouseMove={onMove} onMouseLeave={() => setProbe(null)} />
      <div className="contour-legend">
        <div className="legend-title">{display.label}</div>
        {display.limits && (display.limits[0] || display.limits[1]) && (
          <div className="legend-limits" title="Values beyond typed-in limits take the end colours">
            {display.limits[1] ? "max fixed" : ""}{display.limits[0] && display.limits[1] ? " · " : ""}{display.limits[0] ? "min fixed" : ""}
          </div>
        )}
        {Array.from({ length: n + 1 }, (_, k) => n - k).map((i) => (
          <div className="legend-row-band" key={i}>
            <i style={{ background: i > 0 ? `rgb(${colors[i - 1].map((c) => Math.round(c * 255)).join(",")})` : "transparent" }} />
            <span>{fmtSci(display.min + (span * i) / n)}</span>
          </div>
        ))}
      </div>
      <div className="contour-info">
        {display.title}
        {display.deformScale > 0 && <> · Deformation scale factor: {display.deformScale.toPrecision(3)}</>}
      </div>
      {display.notes.length > 0 && (
        <div className="contour-notes">
          {display.notes.map((t, i) => (
            <div key={i}>{t}</div>
          ))}
        </div>
      )}
      <button className="contour-fit" onClick={() => sceneRef.current && fit(sceneRef.current, display.view)}>
        Fit
      </button>
      {probe && (
        <div className="probe" style={{ left: probe.x + 14, top: probe.y + 14 }}>
          {probe.v === null ? "no value" : fmtSci(probe.v)}
        </div>
      )}
    </div>
  );
}

// Camera directions (from the model towards the camera) for Abaqus' standard views; Y is up.
const VIEW_DIRS: Record<string, [number, number, number]> = {
  Iso: [1, 1, 1], Front: [0, 0, 1], Back: [0, 0, -1], Top: [0, 1, 0.0001], Bottom: [0, -1, 0.0001],
  Left: [-1, 0, 0], Right: [1, 0, 0],
};

export function fit(s: Pick<Scene, "group" | "camera" | "controls">, view?: string) {
  const box = new THREE.Box3().setFromObject(s.group);
  if (box.isEmpty()) return;
  const sphere = box.getBoundingSphere(new THREE.Sphere());
  // Fit the bounding sphere inside whichever field of view is narrower (vertical or horizontal).
  const vfov = THREE.MathUtils.degToRad(s.camera.fov / 2);
  const hfov = Math.atan(Math.tan(vfov) * s.camera.aspect);
  const dist = (sphere.radius / Math.sin(Math.min(vfov, hfov))) * 1.05;
  const dir = new THREE.Vector3(...(VIEW_DIRS[view ?? ""] ?? [0.6, 0.45, 1])).normalize();
  s.camera.position.copy(sphere.center).addScaledVector(dir, dist);
  s.camera.near = dist / 100;
  s.camera.far = dist * 100;
  s.camera.updateProjectionMatrix();
  s.controls.target.copy(sphere.center);
  s.controls.update();
}
