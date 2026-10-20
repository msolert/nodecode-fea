import { useEffect, useMemo, useRef } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { useEditor } from "../../context";
import type { Body3d, Datum3d, Display, Mesh3d } from "../../types";
import { fit } from "./ContourView";

type ModelDisplay = Extract<Display, { kind: "model3d" }>;
type Pt = [number, number];

const FOCUS = 0x4fb3e8;
const OTHER = 0xb8c0cc;
const MARK = 0xff4d4d;
const MESH = 0xc9d6e3;
const MESH_EDGES = 0x2a3446;
const PICKED = 0xff8c1a;
const DATUM = 0xe8c547;

/** Corner-node polygons as triangles (a fan each), unshared so every element face shades flat. */
function polygons(xyz: [number, number, number][], faces: number[][]) {
  const pos: number[] = [];
  for (const f of faces) for (let i = 1; i + 1 < f.length; i++) for (const n of [f[0], f[i], f[i + 1]]) pos.push(...xyz[n]);
  const g = new THREE.BufferGeometry();
  g.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
  g.computeVertexNormals();
  return g;
}

/** The element edges of polygons (each once) and segments, as one line set. */
function segments(xyz: [number, number, number][], faces: number[][], lines: number[][]) {
  const seen = new Set<string>();
  const pos: number[] = [];
  const add = (a: number, b: number) => {
    const k = a < b ? `${a},${b}` : `${b},${a}`;
    if (seen.has(k)) return;
    seen.add(k);
    pos.push(...xyz[a], ...xyz[b]);
  };
  for (const f of faces) f.forEach((n, i) => add(n, f[(i + 1) % f.length]));
  for (const l of lines) for (let i = 0; i + 1 < l.length; i++) add(l[i], l[i + 1]);
  return new THREE.BufferGeometry().setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
}

/** CAE's mesh of the part, with the Region's elements in orange drawn over it. */
function drawMesh(group: THREE.Group, mesh: Mesh3d) {
  const xyz = mesh.nodes ?? [];
  const faces = mesh.faces ?? [], lines = mesh.lines ?? [];
  const h = mesh.highlight;
  const picked = (h?.faces?.length ?? 0) + (h?.lines?.length ?? 0) + (h?.points?.length ?? 0) > 0;
  if (faces.length) {
    group.add(new THREE.Mesh(polygons(xyz, faces), new THREE.MeshPhongMaterial({
      color: MESH, side: THREE.DoubleSide, shininess: 10, polygonOffset: true, polygonOffsetFactor: 2, polygonOffsetUnits: 2,
      transparent: picked, opacity: picked ? 0.8 : 1,
    })));
  }
  group.add(new THREE.LineSegments(segments(xyz, faces, lines),
    new THREE.LineBasicMaterial({ color: faces.length ? MESH_EDGES : FOCUS })));
  if (!h) return;
  if (h.faces?.length) {
    group.add(new THREE.Mesh(polygons(xyz, h.faces), new THREE.MeshPhongMaterial({
      color: PICKED, side: THREE.DoubleSide, shininess: 10, polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1,
    })));
    group.add(new THREE.LineSegments(segments(xyz, h.faces, []), new THREE.LineBasicMaterial({ color: 0x5a2a00 })));
  }
  const over = (o: THREE.Object3D) => {
    o.renderOrder = 2;
    group.add(o);
  };
  if (h.lines?.length) {
    over(new THREE.LineSegments(segments(xyz, [], h.lines), new THREE.LineBasicMaterial({ color: PICKED, depthTest: false })));
  }
  if (h.points?.length) {
    const g = new THREE.BufferGeometry().setAttribute("position",
      new THREE.Float32BufferAttribute(h.points.flatMap((n) => xyz[n]), 3));
    over(new THREE.Points(g, new THREE.PointsMaterial({ color: PICKED, size: 6, sizeAttenuation: false, depthTest: false })));
  }
}

const closedLoop = (l: Pt[]) =>
  l.length >= 4 && Math.hypot(l[0][0] - l[l.length - 1][0], l[0][1] - l[l.length - 1][1]) < 1e-9 * (1 + Math.abs(l[0][0]) + Math.abs(l[0][1]));

function inside(p: Pt, poly: Pt[]) {
  let c = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [xi, yi] = poly[i], [xj, yj] = poly[j];
    if (yi > p[1] !== yj > p[1] && p[0] < ((xj - xi) * (p[1] - yi)) / (yj - yi) + xi) c = !c;
  }
  return c;
}

/** Closed loops as faces: a loop inside an odd number of others is a hole of the smallest one around it. */
function shapes(loops: Pt[][]): THREE.Shape[] {
  const closed = loops.filter(closedLoop);
  const area = (l: Pt[]) => Math.abs(l.reduce((a, p, i) => a + p[0] * l[(i + 1) % l.length][1] - l[(i + 1) % l.length][0] * p[1], 0) / 2);
  const around = closed.map((l, i) => closed.filter((o, k) => k !== i && inside(l[0], o)));
  const out = new Map<Pt[], THREE.Shape>();
  closed.forEach((l, i) => {
    if (around[i].length % 2 === 0) out.set(l, new THREE.Shape(l.map(([x, y]) => new THREE.Vector2(x, y))));
  });
  closed.forEach((l, i) => {
    if (around[i].length % 2 === 0) return;
    const parent = around[i].filter((o) => out.has(o)).sort((a, b) => area(a) - area(b))[0];
    if (parent) out.get(parent)!.holes.push(new THREE.Path(l.map(([x, y]) => new THREE.Vector2(x, y))));
  });
  return [...out.values()];
}

/** The side walls of an extruded outline (a shell has no caps). */
function walls(loops: Pt[][], depth: number) {
  const pos: number[] = [];
  for (const l of loops) {
    for (let i = 0; i + 1 < l.length; i++) {
      const [a, b] = [l[i], l[i + 1]];
      pos.push(a[0], a[1], 0, b[0], b[1], 0, b[0], b[1], depth, a[0], a[1], 0, b[0], b[1], depth, a[0], a[1], depth);
    }
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
  g.computeVertexNormals();
  return g;
}

/** Abaqus revolves the profile about the sketch's y axis, starting in the XY plane. */
function lathe(loop: Pt[], angle: number) {
  const g = new THREE.LatheGeometry(loop.map(([x, y]) => new THREE.Vector2(Math.abs(x), y)), 48, 0, THREE.MathUtils.degToRad(angle));
  g.rotateY(Math.PI / 2);
  return g;
}

/** The meshes (and lines) of one body, in part coordinates. */
function build(b: Body3d): { surfaces: THREE.BufferGeometry[]; lines: THREE.Vector3[][] } {
  const loops = b.loops as Pt[][];
  if (!loops.length && b.points.length) return { surfaces: [], lines: [b.points.map((p) => new THREE.Vector3(...p))] };
  if (b.body === "Wire") return { surfaces: [], lines: loops.map((l) => l.map(([x, y]) => new THREE.Vector3(x, y, 0))) };
  const faces = shapes(loops);
  if (b.method === "Extrude") {
    if (b.body === "Solid") {
      return { surfaces: [new THREE.ExtrudeGeometry(faces, { depth: b.depth, bevelEnabled: false, curveSegments: 1 })], lines: [] };
    }
    return { surfaces: [walls(loops, b.depth)], lines: [] };
  }
  if (b.method === "Revolve") {
    const surfaces: THREE.BufferGeometry[] = loops.filter(closedLoop).map((l) => lathe(l, b.angle));
    if (b.body === "Solid" && b.angle < 360) {
      // the profile closes both ends of a partial revolution
      const start = new THREE.ShapeGeometry(faces);
      const end = new THREE.ShapeGeometry(faces).rotateY(-THREE.MathUtils.degToRad(b.angle));
      surfaces.push(start, end);
    }
    return { surfaces, lines: [] };
  }
  return { surfaces: [new THREE.ShapeGeometry(faces)], lines: [] };
}

/** Datums drawn through the model: a plane as a see-through square over it, an axis as a line across it, a point
 * (and a face's or the defining points) as dots; sized and centred on what is drawn so far. */
function drawDatums(group: THREE.Group, datums: Datum3d[]) {
  const box = new THREE.Box3().setFromObject(group);
  const size = box.isEmpty() ? 100 : box.getSize(new THREE.Vector3()).length() || 100;
  const centre = box.isEmpty() ? new THREE.Vector3() : box.getCenter(new THREE.Vector3());
  const over = (o: THREE.Object3D) => {
    o.renderOrder = 3;
    group.add(o);
  };
  const dot = (p: [number, number, number]) => {
    const m = new THREE.Mesh(new THREE.SphereGeometry(size * 0.022, 16, 12), new THREE.MeshBasicMaterial({ color: DATUM, depthTest: false }));
    m.position.set(...p);
    over(m);
  };
  for (const d of datums) {
    const origin = new THREE.Vector3(...(d.origin ?? [0, 0, 0]));
    if (d.kind === "plane" && d.normal) {
      const n = new THREE.Vector3(...d.normal).normalize();
      if (n.lengthSq() === 0) continue;
      // the model's centre dropped onto the plane, so the square sits over the part
      const at = centre.clone().sub(n.clone().multiplyScalar(centre.clone().sub(origin).dot(n)));
      const side = size * 0.9;
      const q = new THREE.Quaternion().setFromUnitVectors(new THREE.Vector3(0, 0, 1), n);
      const sheet = new THREE.Mesh(new THREE.PlaneGeometry(side, side), new THREE.MeshBasicMaterial({
        color: DATUM, side: THREE.DoubleSide, transparent: true, opacity: 0.3, depthWrite: false,
      }));
      sheet.quaternion.copy(q);
      sheet.position.copy(at);
      over(sheet);
      const rim = new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.PlaneGeometry(side, side)), new THREE.LineBasicMaterial({ color: DATUM }));
      rim.quaternion.copy(q);
      rim.position.copy(at);
      over(rim);
    } else if (d.kind === "axis" && d.direction) {
      const u = new THREE.Vector3(...d.direction).normalize();
      if (u.lengthSq() === 0) continue;
      const at = origin.clone().add(u.clone().multiplyScalar(centre.clone().sub(origin).dot(u)));
      const half = u.clone().multiplyScalar(size * 0.6);
      over(new THREE.Line(new THREE.BufferGeometry().setFromPoints([at.clone().sub(half), at.clone().add(half)]),
        new THREE.LineBasicMaterial({ color: DATUM, depthTest: false })));
    } else if (d.kind === "point" && d.origin) {
      dot(d.origin);
    }
    for (const p of d.points ?? []) dot(p);
  }
}

/** An instance's moves and turns, in order, as one matrix. */
function placement(b: Body3d) {
  const m = new THREE.Matrix4();
  for (const st of b.steps) {
    if (st[0] === "move") {
      m.premultiply(new THREE.Matrix4().makeTranslation(...st[1]));
    } else {
      const [, axis, deg, pivot] = st;
      const dir = new THREE.Vector3(axis === "X" ? 1 : 0, axis === "Y" ? 1 : 0, axis === "Z" ? 1 : 0);
      const p = new THREE.Vector3(...pivot);
      m.premultiply(new THREE.Matrix4().makeTranslation(-p.x, -p.y, -p.z))
        .premultiply(new THREE.Matrix4().makeRotationAxis(dir, THREE.MathUtils.degToRad(deg)))
        .premultiply(new THREE.Matrix4().makeTranslation(p.x, p.y, p.z));
    }
  }
  return m;
}

interface Scene {
  renderer: THREE.WebGLRenderer;
  scene: THREE.Scene;
  camera: THREE.PerspectiveCamera;
  controls: OrbitControls;
  group: THREE.Group;
}

/** Parts and the assembly in 3D, from what the graph builds (no CAE needed): an instance is shown among the
 * model's other instances, each where its last Instance node puts it. */
export function ModelView({ display }: { display: ModelDisplay }) {
  const { results } = useEditor();
  const host = useRef<HTMLDivElement>(null);
  const sceneRef = useRef<Scene | null>(null);
  const fitted = useRef({ key: "", size: 0 });

  const bodies = useMemo(() => {
    if (!display.focus) return display.bodies;
    const placed = new Map<string, Body3d>();
    for (const r of Object.values(results)) {
      const d = r.status === "ok" ? r.display : undefined;
      if (d?.kind !== "model3d" || d.model !== display.model || !d.focus) continue;
      for (const b of d.bodies) {
        const had = placed.get(b.instance);
        if (!had || b.steps.length > had.steps.length) placed.set(b.instance, b);
      }
    }
    for (const b of display.bodies) placed.set(b.instance, b);
    return [...placed.values()];
  }, [display, results]);

  useEffect(() => {
    const el = host.current!;
    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, preserveDrawingBuffer: true });
    renderer.setPixelRatio(window.devicePixelRatio);
    el.appendChild(renderer.domElement);
    const scene = new THREE.Scene();
    scene.add(new THREE.AmbientLight(0xffffff, 1.2));
    const sun = new THREE.DirectionalLight(0xffffff, 1.8);
    sun.position.set(1, 2, 3);
    const camera = new THREE.PerspectiveCamera(35, 1, 0.01, 1e7);
    camera.add(sun);
    scene.add(camera);
    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    const group = new THREE.Group();
    scene.add(group);
    sceneRef.current = { renderer, scene, camera, controls, group };
    const resize = () => {
      const w = el.clientWidth, h = el.clientHeight;
      renderer.setSize(w, h);
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
    };
    loop();
    return () => {
      cancelAnimationFrame(frame);
      ro.disconnect();
      controls.dispose();
      clear(group);
      renderer.dispose();
      el.removeChild(renderer.domElement);
      sceneRef.current = null;
    };
  }, []);

  useEffect(() => {
    const s = sceneRef.current;
    if (!s) return;
    clear(s.group);
    const mesh = display.mesh?.nodes?.length ? display.mesh : undefined;
    if (mesh) drawMesh(s.group, mesh);
    // an instance's mesh is drawn where it is, among the model's other instances (still from their profiles)
    for (const b of mesh ? bodies.filter((o) => display.focus && o.instance !== display.focus) : bodies) {
      const color = !display.focus || b.instance === display.focus ? FOCUS : OTHER;
      const m = placement(b);
      const { surfaces, lines } = build(b);
      for (const g of surfaces) {
        g.applyMatrix4(m);
        const mesh = new THREE.Mesh(g, new THREE.MeshPhongMaterial({
          color, side: THREE.DoubleSide, flatShading: false, shininess: 20,
          transparent: color === OTHER, opacity: color === OTHER ? 0.55 : 1, polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1,
        }));
        s.group.add(mesh);
        s.group.add(new THREE.LineSegments(new THREE.EdgesGeometry(g, 25), new THREE.LineBasicMaterial({ color: 0x1b2333 })));
      }
      for (const l of lines) {
        const g = new THREE.BufferGeometry().setFromPoints(l).applyMatrix4(m);
        s.group.add(new THREE.Line(g, new THREE.LineBasicMaterial({ color })));
      }
    }
    // A Region's points (where it picks faces, edges or vertices), sized to the model, drawn through it.
    if (display.markers?.length) {
      const size = new THREE.Box3().setFromObject(s.group).getSize(new THREE.Vector3()).length() || 1;
      for (const p of display.markers) {
        const dot = new THREE.Mesh(new THREE.SphereGeometry(size * 0.012, 16, 12),
          new THREE.MeshBasicMaterial({ color: MARK, depthTest: false }));
        dot.position.set(...p);
        dot.renderOrder = 1;
        s.group.add(dot);
      }
    }
    if (display.datums?.length) drawDatums(s.group, display.datums);
    // Fit once per thing shown, then keep the camera where the user left it while values change, unless the
    // model grew or shrank a lot (another method, other units).
    const key = `${display.model}|${display.focus || display.bodies[0]?.part}|${bodies.map((b) => b.method + b.body).join()}`;
    const size = new THREE.Box3().setFromObject(s.group).getSize(new THREE.Vector3()).length();
    const ratio = size / (fitted.current.size || size);
    if (fitted.current.key !== key || ratio > 2 || ratio < 0.5) {
      fit(s);
      fitted.current = { key, size };
    }
  }, [bodies, display]);

  const m = display.mesh;
  const datumNotes = (display.datums ?? []).map((d) =>
    d.kind === "face" ? `${d.name}: offset from a face, placed by CAE (the face's picked points in yellow)` : `${d.name} in yellow`);
  const notes = m?.nodes?.length
    ? [`CAE mesh: ${m.elements ?? 0} elements` + (m.highlight?.elements ? `, ${m.highlight.elements} in the region (orange)` : "")]
        .concat(m.highlight?.note ? [m.highlight.note] : [])
        .concat(datumNotes)
    : bodies.flatMap((b) => (b.features.length ? [`${b.part}: ${b.features.join(", ")} not drawn`] : []))
        .concat(m?.too_big ? [`The CAE mesh (${m.too_big} elements) is too big to preview here`] : [])
        .concat(datumNotes);
  return (
    <div className="contour model3d">
      <div className="contour-canvas" ref={host} />
      <div className="contour-info">{display.title}</div>
      {notes.length > 0 && (
        <div className="contour-notes">
          {notes.map((t, i) => (
            <div key={i}>{t}</div>
          ))}
        </div>
      )}
      <button className="contour-fit" onClick={() => sceneRef.current && fit(sceneRef.current)}>
        Fit
      </button>
    </div>
  );
}

function clear(group: THREE.Group) {
  group.traverse((o) => {
    const m = o as THREE.Mesh;
    m.geometry?.dispose();
    (m.material as THREE.Material | undefined)?.dispose?.();
  });
  group.clear();
}
