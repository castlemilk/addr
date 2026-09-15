"use client";

import React, { useEffect, useRef, useState, useCallback } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import {
  generateDeep3DHeightmap,
  loadSatelliteImageOrFallback,
  Deep3DModelOutput,
  Deep3DBuildingData,
  Deep3DFootprint,
} from "@/lib/deep-3d-model";
import { SatelliteVintage } from "@/lib/google-maps-style";
import { buildRoofSections } from "@/lib/roof-geometry";
import { findAddressRoof, loadAddressRoofs } from "@/lib/address-roofs";
import {
  X,
  Box,
  Sparkles,
  RotateCcw,
  RotateCw,
  Play,
  Pause,
  Download,
  Sliders,
  Flame,
} from "lucide-react";

export type Deep3DRenderMode = "textured" | "heatmap" | "wireframe" | "clay" | "hybrid";
type Deep3DRoofType = "hipped" | "gabled" | "executive" | "flat";

interface Deep3DStudioProps {
  isOpen: boolean;
  onClose: () => void;
  targetLat?: number;
  targetLon?: number;
  addressTitle?: string;
  initialVintage?: SatelliteVintage;
  building?: Deep3DBuildingData | null;
}

const TILE_ZOOM = 18;

interface BuildingPlacement {
  footprint: Deep3DFootprint;
  metersPerWidth: number;
  metersPerHeight: number;
  offsetEast: number;
  offsetNorth: number;
}

function buildingPlacement(building: Deep3DBuildingData): BuildingPlacement {
  const tileX = Math.floor(((building.centroid.lon + 180) / 360) * 2 ** TILE_ZOOM);
  const latRad = (building.centroid.lat * Math.PI) / 180;
  const tileY = Math.floor(
    ((1 - Math.log(Math.tan(latRad) + 1 / Math.cos(latRad)) / Math.PI) / 2) * 2 ** TILE_ZOOM
  );
  const tileCenterLon = ((tileX + 0.5) / 2 ** TILE_ZOOM) * 360 - 180;
  const tileCenterLat =
    (Math.atan(Math.sinh(Math.PI * (1 - (2 * (tileY + 0.5)) / 2 ** TILE_ZOOM))) * 180) / Math.PI;

  const cosLat = Math.cos((building.centroid.lat * Math.PI) / 180);
  const metersPerWidth = (40075016.686 * cosLat) / 2 ** TILE_ZOOM;
  const metersPerHeight = metersPerWidth;
  const offsetEast = (building.centroid.lon - tileCenterLon) * 111320 * cosLat;
  const offsetNorth = (building.centroid.lat - tileCenterLat) * 111320;

  const ring = building.ringMeters.map(
    ([mx, my]) =>
      [
        0.5 + (offsetEast + mx) / metersPerWidth,
        0.5 - (offsetNorth + my) / metersPerHeight,
      ] as [number, number]
  );

  return {
    footprint: {
      ring,
      ridgeAngleDeg: building.ridgeAngle,
      widthMeters: building.widthMeters,
      depthMeters: building.depthMeters,
      areaM2: building.areaM2,
    },
    metersPerWidth,
    metersPerHeight,
    offsetEast,
    offsetNorth,
  };
}

function sceneScaleFor(placement: BuildingPlacement): number {
  return 32 / Math.max(1, placement.metersPerWidth);
}

function verticalScaleFor(placement: BuildingPlacement, depthScale: number): number {
  return sceneScaleFor(placement) * depthScale * 1.5;
}

const PROPERTY_PRESETS = [{
    id: "active",
    name: "Active Location",
    desc: "Currently selected map address",
    lat: -37.79044,
    lon: 145.31252,
    roofType: "hipped" as const,
    eave: 5.5,
    ridge: 8.0,
  },
  {
    id: "cambridge",
    name: "27 Cambridge Rd",
    desc: "Mooroolbark Suburban Parcel",
    lat: -37.79044,
    lon: 145.31252,
    roofType: "hipped" as const,
    eave: 5.5,
    ridge: 8.0,
  },
  {
    id: "cbd",
    name: "Melbourne CBD",
    desc: "High-Rise Commercial Envelope",
    lat: -37.8136,
    lon: 144.9631,
    roofType: "executive" as const,
    eave: 12.0,
    ridge: 24.0,
  },
  {
    id: "hawthorn",
    name: "Hawthorn Heritage Villa",
    desc: "Victorian Pitched Gable Residence",
    lat: -37.8228,
    lon: 145.0345,
    roofType: "gabled" as const,
    eave: 5.5,
    ridge: 9.0,
  },
];

export default function Deep3DStudio({
  isOpen,
  onClose,
  targetLat = -37.79044,
  targetLon = 145.31252,
  addressTitle = "27 Cambridge Road, Mooroolbark VIC 3138",
  initialVintage = "google-latest",
  building = null,
}: Deep3DStudioProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  // WebGL scene references
  const sceneRef = useRef<THREE.Scene | null>(null);
  const cameraRef = useRef<THREE.PerspectiveCamera | null>(null);
  const rendererRef = useRef<THREE.WebGLRenderer | null>(null);
  const controlsRef = useRef<OrbitControls | null>(null);
  const meshRef = useRef<THREE.Mesh | null>(null);
  const wireframeMeshRef = useRef<THREE.Mesh | null>(null);
  const geometryRef = useRef<THREE.PlaneGeometry | null>(null);
  const texturesRef = useRef<{
    satellite: THREE.Texture | null;
    heatmap: THREE.Texture | null;
  }>({ satellite: null, heatmap: null });
  const animFrameIdRef = useRef<number | null>(null);

  // 3D Engine States
  const [depthScale, setDepthScale] = useState<number>(1.25);
  const depthScaleRef = useRef<number>(1.25);
  const [renderMode, setRenderMode] = useState<Deep3DRenderMode>("textured");
  const [roofType, setRoofType] = useState<Deep3DRoofType>("hipped");
  const [eaveHeight, setEaveHeight] = useState<number>(5.5);
  const [ridgeHeight, setRidgeHeight] = useState<number>(8.0);
  const [vintage, setVintage] = useState<SatelliteVintage>(initialVintage);
  const [autoRotate, setAutoRotate] = useState<boolean>(false);

  const [activeLat, setActiveLat] = useState<number>(targetLat);
  const [activeLon, setActiveLon] = useState<number>(targetLon);
  const [activePreset, setActivePreset] = useState<string>("active");
  const [activeBuilding, setActiveBuilding] = useState<Deep3DBuildingData | null>(building);

  const placementRef = useRef<BuildingPlacement | null>(null);
  const runIdRef = useRef<number>(0);
  const groundRefs = useRef<{ base: THREE.Mesh | null; grid: THREE.GridHelper | null }>({
    base: null,
    grid: null,
  });
  const buildingMeshRef = useRef<THREE.Group | null>(null);
  const buildingMaterialsRef = useRef<{
    wall: THREE.MeshStandardMaterial;
    roof: THREE.MeshStandardMaterial;
  } | null>(null);

  const [isProcessing, setIsProcessing] = useState<boolean>(true);
  const [modelOutput, setModelOutput] = useState<Deep3DModelOutput | null>(null);
  const [crossSectionData, setCrossSectionData] = useState<number[]>([]);
  const [footprintNotice, setFootprintNotice] = useState<string | null>(null);

  // Update target when props change
  useEffect(() => {
    setActiveLat(targetLat);
    setActiveLon(targetLon);
  }, [targetLat, targetLon]);

  // Adopt the real building footprint when the map selection provides one
  useEffect(() => {
    setActiveBuilding(building ?? null);
    if (building) {
      setRoofType(building.roofType);
      setEaveHeight(building.eaveHeight);
      setRidgeHeight(building.ridgeHeight);
      setFootprintNotice(null);
    }
  }, [building]);

  // If the map could not resolve a footprint, fall back to the nearest
  // per-address record so the studio always shows a real roof when one exists
  useEffect(() => {
    if (!isOpen || building) return;
    let cancelled = false;
    loadAddressRoofs().then((records) => {
      if (cancelled) return;
      const match = findAddressRoof(records, targetLon, targetLat, 45);
      if (!match) {
        setFootprintNotice("none");
        return;
      }
      setActiveBuilding(match.building);
      setRoofType(match.building.roofType);
      setEaveHeight(match.building.eaveHeight);
      setRidgeHeight(match.building.ridgeHeight);
      setFootprintNotice(
        `Using nearest address footprint: ${match.record.full_address} (${Math.round(
          match.record.distance_meters
        )} m away)`
      );
    });
    return () => {
      cancelled = true;
    };
  }, [isOpen, building, targetLat, targetLon]);

  // 2. Displace WebGL Geometry Vertices
  const updateMeshDisplacement = (
    geom: THREE.PlaneGeometry,
    heightfield: Float32Array,
    scale: number
  ) => {
    const pos = geom.attributes.position;
    const vertexCount = pos.count;
    const placement = placementRef.current;
    // Same horizontal/vertical scale as the roof mesh so the extracted roof
    // bump and the reconstructed mesh stay locked together.
    const verticalScale = placement
      ? verticalScaleFor(placement, scale)
      : 0.5;

    for (let i = 0; i < vertexCount; i++) {
      const h = heightfield[i] ?? 0;
      // In Y-up convention (rotated PlaneGeometry): Y is elevation
      pos.setY(i, h * verticalScale);
    }

    pos.needsUpdate = true;
    geom.computeVertexNormals();
  };

  // 3. Apply active Material mode
  const applyMaterial = useCallback(
    (mode: Deep3DRenderMode) => {
      if (!meshRef.current) return;

      const { satellite, heatmap } = texturesRef.current;

      if (mode === "textured" && satellite) {
        meshRef.current.material = new THREE.MeshStandardMaterial({
          map: satellite,
          roughness: 0.6,
          metalness: 0.1,
          side: THREE.DoubleSide,
        });
        if (wireframeMeshRef.current) wireframeMeshRef.current.visible = false;
      } else if (mode === "heatmap" && heatmap) {
        meshRef.current.material = new THREE.MeshStandardMaterial({
          map: heatmap,
          roughness: 0.45,
          metalness: 0.0,
          side: THREE.DoubleSide,
        });
        if (wireframeMeshRef.current) wireframeMeshRef.current.visible = false;
      } else if (mode === "wireframe") {
        meshRef.current.material = new THREE.MeshBasicMaterial({
          color: 0x38bdf8,
          wireframe: true,
        });
        if (wireframeMeshRef.current) wireframeMeshRef.current.visible = false;
      } else if (mode === "clay") {
        meshRef.current.material = new THREE.MeshStandardMaterial({
          color: 0xf1f5f9,
          roughness: 0.4,
          metalness: 0.1,
          side: THREE.DoubleSide,
        });
        if (wireframeMeshRef.current) wireframeMeshRef.current.visible = false;
      } else if (mode === "hybrid" && satellite) {
        meshRef.current.material = new THREE.MeshStandardMaterial({
          map: satellite,
          roughness: 0.6,
          metalness: 0.1,
          side: THREE.DoubleSide,
        });
        if (wireframeMeshRef.current) {
          wireframeMeshRef.current.visible = true;
        }
      }
    },
    []
  );

  // 1. Run Monocular Height Estimation Model
  const rebuildBuildingMesh = useCallback(
    (bld: Deep3DBuildingData | null) => {
      const group = buildingMeshRef.current;
      const materials = buildingMaterialsRef.current;
      if (!group || !materials) return;
      for (const child of [...group.children]) {
        group.remove(child);
        if (child instanceof THREE.Mesh) child.geometry.dispose();
      }
      if (!bld) return;

      const placement = placementRef.current ?? buildingPlacement(bld);
      const sceneUnitsPerMeter = sceneScaleFor(placement);
      const verticalScale = verticalScaleFor(placement, depthScaleRef.current);
      const { wall, roof } = buildRoofSections({
        ring: bld.ringMeters,
        eave: bld.eaveHeight,
        ridge: bld.ridgeHeight,
        roofType: bld.roofType,
        ridgeAngleDeg: bld.ridgeAngle,
      });
      const toScene = (positions: number[]): THREE.BufferGeometry | null => {
        if (positions.length === 0) return null;
        const out = new Float32Array(positions.length);
        for (let index = 0; index < positions.length; index += 3) {
          out[index] = positions[index] * sceneUnitsPerMeter;
          out[index + 1] = positions[index + 2] * verticalScale + 0.06;
          out[index + 2] = -positions[index + 1] * sceneUnitsPerMeter;
        }
        const geometry = new THREE.BufferGeometry();
        geometry.setAttribute("position", new THREE.Float32BufferAttribute(out, 3));
        geometry.computeVertexNormals();
        return geometry;
      };
      const wallGeometry = toScene(wall);
      const roofGeometry = toScene(roof);
      if (wallGeometry) group.add(new THREE.Mesh(wallGeometry, materials.wall));
      if (roofGeometry) {
        materials.roof.color.set(
          bld.roofType === "gabled" ? 0xa84a2d : bld.roofType === "flat" ? 0x7c8794 : 0x4b5563
        );
        group.add(new THREE.Mesh(roofGeometry, materials.roof));
      }
      console.info(
        `[Deep3D] roof mesh: ${wallGeometry ? wall.length / 9 : 0} wall tris, ` +
          `${roofGeometry ? roof.length / 9 : 0} roof tris, scale ${sceneUnitsPerMeter.toFixed(3)} u/m`
      );
    },
    []
  );

  const runModel = useCallback(
    async (
      lat: number,
      lon: number,
      rType: Deep3DRoofType,
      eH: number,
      rH: number,
      vnt: SatelliteVintage,
      bld: Deep3DBuildingData | null
    ) => {
      setIsProcessing(true);
      const runId = ++runIdRef.current;
      try {
        const placement = bld ? buildingPlacement(bld) : null;
        placementRef.current = placement;

        // Load real satellite tile or high-res procedural orthophoto
        const imgSource = await loadSatelliteImageOrFallback(
          bld?.centroid.lat ?? lat,
          bld?.centroid.lon ?? lon,
          vnt
        );
        if (runId !== runIdRef.current) return;

        const output = await generateDeep3DHeightmap({
          imageSource: imgSource,
          resolution: 128,
          eaveHeight: eH,
          ridgeHeight: rH,
          roofType: rType,
          footprint: placement?.footprint,
          metersPerWidth: placement?.metersPerWidth,
          metersPerHeight: placement?.metersPerHeight,
        });
        if (runId !== runIdRef.current) return;

        setModelOutput(output);

        // Extract the cross-section through the building when known
        const slice: number[] = [];
        const res = output.resolution;
        let row = Math.floor(res / 2);
        if (bld && placement) {
          const v = 0.5 - placement.offsetNorth / placement.metersPerHeight;
          row = Math.max(0, Math.min(res - 1, Math.round(v * (res - 1))));
        }
        for (let col = 0; col < res; col += 2) {
          slice.push(output.heightfield[row * res + col]);
        }
        setCrossSectionData(slice);

        // Update WebGL Textures & Mesh
        if (sceneRef.current && geometryRef.current) {
          // Dispose old textures
          if (texturesRef.current.satellite) texturesRef.current.satellite.dispose();
          if (texturesRef.current.heatmap) texturesRef.current.heatmap.dispose();

          const satImg = new Image();
          satImg.src = output.satelliteDataUrl;
          await satImg.decode().catch(() => {});

          const heatImg = new Image();
          heatImg.src = output.depthColorDataUrl;
          await heatImg.decode().catch(() => {});
          if (runId !== runIdRef.current) return;

          const satTex = new THREE.Texture(satImg);
          satTex.colorSpace = THREE.SRGBColorSpace;
          satTex.generateMipmaps = true;
          satTex.needsUpdate = true;

          const heatTex = new THREE.Texture(heatImg);
          heatTex.colorSpace = THREE.SRGBColorSpace;
          heatTex.generateMipmaps = true;
          heatTex.needsUpdate = true;

          texturesRef.current = { satellite: satTex, heatmap: heatTex };

          // Displace geometry vertices and centre the model on the building
          updateMeshDisplacement(geometryRef.current, output.heightfield, depthScale);
          applyMaterial(renderMode);
        }

        rebuildBuildingMesh(bld);
      } catch (err) {
        console.error("Deep 3D Model computation failed:", err);
      } finally {
        setIsProcessing(false);
      }
    },
    [depthScale, renderMode, applyMaterial, rebuildBuildingMesh]
  );

  // Re-frame the scene around the real building footprint when it changes
  useEffect(() => {
    const placement = placementRef.current;
    const camera = cameraRef.current;
    const controls = controlsRef.current;
    const mesh = meshRef.current;
    const wire = wireframeMeshRef.current;
    if (!placement || !camera || !controls) return;

    const sceneUnitsPerMeter = sceneScaleFor(placement);
    const verticalScale = verticalScaleFor(placement, depthScaleRef.current);
    const offsetX = -placement.offsetEast * sceneUnitsPerMeter;
    const offsetZ = placement.offsetNorth * sceneUnitsPerMeter;

    if (mesh) mesh.position.set(offsetX, 0, offsetZ);
    if (wire) wire.position.set(offsetX, 0.02, offsetZ);
    groundRefs.current.base?.position.set(offsetX, -0.3, offsetZ);
    groundRefs.current.grid?.position.set(offsetX, -0.01, offsetZ);

    const spanMeters = Math.max(
      placement.footprint.widthMeters,
      placement.footprint.depthMeters
    );
    const span = spanMeters * sceneUnitsPerMeter;
    const ridgeScene = Math.max(0.5, (ridgeHeight - 0) * verticalScale);
    const distance = Math.max(8, span * 2.0, ridgeScene * 3.0);

    camera.position.set(0, distance * 0.5, distance * 0.8);
    controls.target.set(0, Math.max(0.5, ridgeScene * 0.35), 0);
    controls.minDistance = Math.max(3, span * 0.35);
    controls.maxDistance = Math.max(30, distance * 3.2);
    controls.update();
  }, [modelOutput, ridgeHeight]);

  // Re-run the model with the real footprint whenever the context changes
  useEffect(() => {
    if (!isOpen) return;
    runModel(activeLat, activeLon, roofType, eaveHeight, ridgeHeight, vintage, activeBuilding);
  }, [activeBuilding]);

  // 4. Initialize Three.js Viewport
  useEffect(() => {
    if (!isOpen || !canvasRef.current) return;

    const canvas = canvasRef.current;
    const width = canvas.parentElement?.clientWidth || 800;
    const height = canvas.parentElement?.clientHeight || 600;

    // Scene
    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x090d16);
    sceneRef.current = scene;

    // Camera
    const camera = new THREE.PerspectiveCamera(42, width / height, 0.1, 1000);
    camera.position.set(0, 26, 32);
    camera.lookAt(0, 4, 0);
    cameraRef.current = camera;

    // Renderer
    const renderer = new THREE.WebGLRenderer({
      canvas,
      antialias: true,
      preserveDrawingBuffer: true,
    });
    renderer.setSize(width, height);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 1.15;
    rendererRef.current = renderer;

    // OrbitControls
    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.06;
    controls.maxPolarAngle = Math.PI / 2 - 0.05; // Prevent camera from dipping below ground
    controls.minDistance = 8;
    controls.maxDistance = 90;
    controls.target.set(0, 4, 0);
    controlsRef.current = controls;

    // Lighting
    const ambientLight = new THREE.AmbientLight(0xffffff, 0.85);
    scene.add(ambientLight);

    const sunLight = new THREE.DirectionalLight(0xfffaed, 1.8);
    sunLight.position.set(24, 38, 20);
    scene.add(sunLight);

    const fillLight = new THREE.DirectionalLight(0x7dd3fc, 0.5);
    fillLight.position.set(-24, 18, -20);
    scene.add(fillLight);

    // Architectural Ground Base / Pedestal
    const baseGeo = new THREE.BoxGeometry(32.4, 0.6, 32.4);
    const baseMat = new THREE.MeshStandardMaterial({
      color: 0x1e293b,
      roughness: 0.7,
      metalness: 0.2,
    });
    const baseMesh = new THREE.Mesh(baseGeo, baseMat);
    baseMesh.position.set(0, -0.3, 0);
    scene.add(baseMesh);
    groundRefs.current.base = baseMesh;

    // Subtle Ground Grid Helper
    const grid = new THREE.GridHelper(32, 16, 0x38bdf8, 0x1e293b);
    grid.position.set(0, -0.01, 0);
    scene.add(grid);
    groundRefs.current.grid = grid;

    // 3D Heightfield Surface Mesh (128x128 = 16,384 vertices)
    const geom = new THREE.PlaneGeometry(32, 32, 127, 127);
    geom.rotateX(-Math.PI / 2); // Lay flat on XZ plane, Y is height
    geometryRef.current = geom;

    const initialMat = new THREE.MeshStandardMaterial({
      color: 0x334155,
      wireframe: false,
      side: THREE.DoubleSide,
    });
    const mesh = new THREE.Mesh(geom, initialMat);
    mesh.position.set(0, 0, 0);
    scene.add(mesh);
    meshRef.current = mesh;

    // Wireframe overlay mesh (for Hybrid mode)
    const wireMat = new THREE.MeshBasicMaterial({
      color: 0x38bdf8,
      wireframe: true,
      transparent: true,
      opacity: 0.35,
    });
    const wireMesh = new THREE.Mesh(geom, wireMat);
    wireMesh.position.set(0, 0.02, 0);
    wireMesh.visible = false;
    scene.add(wireMesh);
    wireframeMeshRef.current = wireMesh;

    // Real extracted roof mesh (walls + pitched roof planes) for the selected address
    const buildingGroup = new THREE.Group();
    const buildingMaterials = {
      wall: new THREE.MeshStandardMaterial({
        color: 0xe9e5dd,
        roughness: 0.85,
        metalness: 0.02,
        side: THREE.DoubleSide,
        flatShading: true,
      }),
      roof: new THREE.MeshStandardMaterial({
        color: 0xa84a2d,
        roughness: 0.7,
        metalness: 0.08,
        side: THREE.DoubleSide,
        flatShading: true,
      }),
    };
    buildingMeshRef.current = buildingGroup;
    buildingMaterialsRef.current = buildingMaterials;
    scene.add(buildingGroup);

    // Initial Model Run
    runModel(activeLat, activeLon, roofType, eaveHeight, ridgeHeight, vintage, building);

    // Render loop
    let isMounted = true;
    const animate = () => {
      if (!isMounted) return;
      controls.autoRotate = autoRotate;
      controls.autoRotateSpeed = 2.0;
      controls.update();
      renderer.render(scene, camera);
      animFrameIdRef.current = requestAnimationFrame(animate);
    };
    animate();

    // Resize Handler
    const handleResize = () => {
      if (!canvas.parentElement) return;
      const w = canvas.parentElement.clientWidth;
      const h = canvas.parentElement.clientHeight;
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
      renderer.setSize(w, h);
    };
    window.addEventListener("resize", handleResize);

    return () => {
      isMounted = false;
      window.removeEventListener("resize", handleResize);
      if (animFrameIdRef.current) cancelAnimationFrame(animFrameIdRef.current);
      controls.dispose();
      renderer.dispose();
      geom.dispose();
      baseGeo.dispose();
      baseMat.dispose();
      initialMat.dispose();
      wireMat.dispose();
      buildingMaterialsRef.current?.wall.dispose();
      buildingMaterialsRef.current?.roof.dispose();
      buildingMeshRef.current?.children.forEach((child) => {
        if (child instanceof THREE.Mesh) child.geometry.dispose();
      });
      buildingMeshRef.current = null;
      buildingMaterialsRef.current = null;
    };
  }, [isOpen]);

  // Update Displacement Scale dynamically in real time
  useEffect(() => {
    depthScaleRef.current = depthScale;
    if (geometryRef.current && modelOutput) {
      updateMeshDisplacement(geometryRef.current, modelOutput.heightfield, depthScale);
    }
    if (isOpen) {
      rebuildBuildingMesh(activeBuilding);
    }
  }, [depthScale, modelOutput, isOpen, activeBuilding, rebuildBuildingMesh]);

  // Update Material dynamically
  useEffect(() => {
    applyMaterial(renderMode);
  }, [renderMode, applyMaterial]);

  // Update Auto-Rotate
  useEffect(() => {
    if (controlsRef.current) {
      controlsRef.current.autoRotate = autoRotate;
    }
  }, [autoRotate]);

  // Handle Preset Changes
  const handlePresetSelect = (presetId: string) => {
    setActivePreset(presetId);
    const preset = PROPERTY_PRESETS.find((p) => p.id === presetId);
    if (preset) {
      setActiveLat(preset.lat);
      setActiveLon(preset.lon);
      setRoofType(preset.roofType);
      setEaveHeight(preset.eave);
      setRidgeHeight(preset.ridge);
      setActiveBuilding(null);
      setFootprintNotice(presetId === "active" ? null : "none");
      runModel(preset.lat, preset.lon, preset.roofType, preset.eave, preset.ridge, vintage, null);
    }
  };

  // Orbit the camera around the current target
  const rotateCamera = (deltaRadians: number) => {
    const camera = cameraRef.current;
    const controls = controlsRef.current;
    if (!camera || !controls) return;
    const offset = camera.position.clone().sub(controls.target);
    offset.applyAxisAngle(new THREE.Vector3(0, 1, 0), deltaRadians);
    camera.position.copy(controls.target.clone().add(offset));
    controls.update();
  };

  // Reset Camera View
  const handleResetCamera = () => {    const camera = cameraRef.current;
    const controls = controlsRef.current;
    if (!camera || !controls) return;
    const placement = placementRef.current;
    const sceneUnitsPerMeter = placement ? sceneScaleFor(placement) : 1;
    const verticalScale = placement ? verticalScaleFor(placement, depthScaleRef.current) : 0.5;
    const span = placement
      ? Math.max(placement.footprint.widthMeters, placement.footprint.depthMeters) *
        sceneUnitsPerMeter
      : 32;
    const ridgeScene = Math.max(0.5, ridgeHeight * verticalScale);
    const distance = Math.max(8, span * 2.0, ridgeScene * 3.0);
    camera.position.set(0, distance * 0.5, distance * 0.8);
    controls.target.set(0, Math.max(0.5, ridgeScene * 0.35), 0);
    controls.update();
  };

  // Take High-Res Canvas Snapshot
  const handleExportSnapshot = () => {
    if (!rendererRef.current) return;
    const dataUrl = rendererRef.current.domElement.toDataURL("image/png");
    const link = document.createElement("a");
    link.download = `deep3d-mesh-${Date.now()}.png`;
    link.href = dataUrl;
    link.click();
  };

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/85 backdrop-blur-md p-2 sm:p-4 animate-in fade-in duration-200">
      <div className="relative flex flex-col w-full h-full max-w-7xl bg-slate-900 border border-slate-700/80 rounded-3xl shadow-2xl overflow-hidden">
        {/* Top Header Bar */}
        <div className="flex items-center justify-between px-5 py-3.5 border-b border-slate-800 bg-slate-900/90 backdrop-blur z-20">
          <div className="flex items-center space-x-3">
            <div className="flex items-center justify-center w-9 h-9 rounded-2xl bg-gradient-to-tr from-blue-600 to-cyan-400 text-white shadow-lg shadow-blue-500/20">
              <Box className="w-5 h-5" />
            </div>
            <div>
              <div className="flex items-center space-x-2">
                <h2 className="text-base font-bold text-white tracking-tight">
                  Deep 3D Satellite Studio
                </h2>
                <span className="px-2 py-0.5 rounded-full text-[10px] font-mono font-semibold bg-blue-500/20 text-blue-300 border border-blue-500/30">
                  Monocular Elevation v2.4
                </span>
                {activeBuilding?.kind && (
                  <span className="px-2 py-0.5 rounded-full text-[10px] font-mono font-semibold bg-emerald-500/20 text-emerald-300 border border-emerald-500/30 uppercase">
                    {activeBuilding.kind}
                  </span>
                )}
              </div>
              <p className="text-xs text-slate-400 truncate max-w-md">
                {addressTitle} • {activeLat.toFixed(5)}, {activeLon.toFixed(5)}
              </p>
            </div>
          </div>

          {/* Quick Presets & Close Button */}
          <div className="flex items-center space-x-2">
            <div className="hidden lg:flex items-center space-x-1 bg-slate-800/80 p-1 rounded-xl border border-slate-700/60 text-xs">
              {PROPERTY_PRESETS.map((p) => (
                <button
                  key={p.id}
                  onClick={() => handlePresetSelect(p.id)}
                  className={`px-2.5 py-1 rounded-lg font-medium transition ${
                    activePreset === p.id
                      ? "bg-blue-600 text-white shadow-sm"
                      : "text-slate-400 hover:text-slate-200"
                  }`}
                >
                  {p.name}
                </button>
              ))}
            </div>

            <button
              onClick={handleExportSnapshot}
              className="p-2 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-300 hover:text-white transition border border-slate-700"
              title="Download 3D Snapshot"
            >
              <Download className="w-4 h-4" />
            </button>

            <button
              onClick={onClose}
              className="p-2 rounded-xl bg-slate-800 hover:bg-red-600/80 text-slate-300 hover:text-white transition border border-slate-700"
              title="Close Deep 3D Studio"
            >
              <X className="w-5 h-5" />
            </button>
          </div>
        </div>

        {/* Main Content Body */}
        <div className="relative flex-1 flex overflow-hidden">
          {/* Central 3D Canvas Viewport */}
          <div className="relative flex-1 h-full w-full bg-slate-950">
            <canvas ref={canvasRef} className="w-full h-full block cursor-grab active:cursor-grabbing" />

            {/* Processing Indicator */}
            {isProcessing && (
              <div className="absolute top-4 left-4 z-20 flex items-center space-x-2 px-3 py-1.5 rounded-full bg-slate-900/90 backdrop-blur border border-blue-500/40 text-xs text-blue-300 shadow-xl">
                <Sparkles className="w-3.5 h-3.5 animate-spin text-blue-400" />
                <span>Computing Heightfield & Canopy Priors...</span>
              </div>
            )}

            {/* Footprint resolution notice */}
            {!isProcessing && footprintNotice === "none" && (
              <div className="absolute top-4 left-1/2 -translate-x-1/2 z-20 px-3 py-1.5 rounded-full bg-amber-500/95 text-slate-950 text-xs font-semibold shadow-xl">
                No extracted roof footprint near this location — showing terrain only.
                Pick a building on the map and reopen.
              </div>
            )}
            {!isProcessing && footprintNotice && footprintNotice !== "none" && (
              <div className="absolute top-4 left-1/2 -translate-x-1/2 z-20 px-3 py-1.5 rounded-full bg-slate-900/90 backdrop-blur border border-blue-500/40 text-blue-200 text-xs shadow-xl">
                {footprintNotice}
              </div>
            )}

            {/* Floating Quick View Controls (Bottom Center of Canvas) */}
            <div className="absolute bottom-4 left-1/2 -translate-x-1/2 z-20 flex items-center space-x-2 px-3 py-2 rounded-2xl bg-slate-900/90 backdrop-blur-md border border-slate-700/80 shadow-2xl">
              <button
                onClick={() => setAutoRotate(!autoRotate)}
                className={`flex items-center space-x-1 px-3 py-1.5 rounded-xl text-xs font-semibold transition ${
                  autoRotate
                    ? "bg-blue-600 text-white shadow-md shadow-blue-500/30"
                    : "bg-slate-800 text-slate-300 hover:bg-slate-700"
                }`}
                title="Continuous 360° Orbit"
              >
                {autoRotate ? <Pause className="w-3.5 h-3.5" /> : <Play className="w-3.5 h-3.5" />}
                <span>Auto-Orbit</span>
              </button>

              <button
                onClick={() => rotateCamera(-Math.PI / 4)}
                className="flex items-center space-x-1 px-2.5 py-1.5 rounded-xl bg-slate-800 text-slate-300 hover:bg-slate-700 text-xs font-semibold transition"
                title="Orbit 45° counter-clockwise"
              >
                <RotateCcw className="w-3.5 h-3.5" />
              </button>

              <button
                onClick={() => rotateCamera(Math.PI / 4)}
                className="flex items-center space-x-1 px-2.5 py-1.5 rounded-xl bg-slate-800 text-slate-300 hover:bg-slate-700 text-xs font-semibold transition"
                title="Orbit 45° clockwise"
              >
                <RotateCw className="w-3.5 h-3.5" />
              </button>

              <button
                onClick={handleResetCamera}
                className="flex items-center space-x-1 px-3 py-1.5 rounded-xl bg-slate-800 text-slate-300 hover:bg-slate-700 text-xs font-semibold transition"
                title="Reset Camera to 45° Perspective"
              >
                <RotateCcw className="w-3.5 h-3.5" />
                <span>Reset View</span>
              </button>

              <div className="h-4 w-px bg-slate-700 mx-1" />

              <span className="text-[11px] text-slate-400 font-mono">
                Left Drag: Rotate • Right Drag: Pan • Scroll: Zoom
              </span>
            </div>
          </div>

          {/* Left Floating Controls Drawer */}
          <div className="absolute top-4 left-4 z-20 w-80 max-h-[calc(100%-2rem)] overflow-y-auto rounded-2xl bg-slate-900/95 backdrop-blur-xl border border-slate-700/80 p-4 shadow-2xl space-y-4 text-slate-200">
            {/* 1. Depth Displacement Relief Slider (Core Feature) */}
            <div className="space-y-2 bg-slate-950/60 p-3.5 rounded-xl border border-slate-800">
              <div className="flex items-center justify-between">
                <div className="flex items-center space-x-2 text-xs font-bold uppercase tracking-wider text-blue-400">
                  <Sliders className="w-3.5 h-3.5 text-blue-400" />
                  <span>3D Depth Relief</span>
                </div>
                <span className="font-mono text-sm font-bold text-white bg-blue-600/30 px-2 py-0.5 rounded-md border border-blue-500/30">
                  {depthScale.toFixed(2)}x
                </span>
              </div>

              <p className="text-[11px] text-slate-400 leading-tight">
                Controls physical Z-axis vertical vertex displacement from flat 2D to deep 3D relief.
              </p>

              <input
                type="range"
                min="0.0"
                max="3.0"
                step="0.05"
                value={depthScale}
                onChange={(e) => setDepthScale(parseFloat(e.target.value))}
                className="w-full h-2 bg-slate-800 rounded-lg appearance-none cursor-pointer accent-blue-500"
              />

              {/* Preset Scale Buttons */}
              <div className="grid grid-cols-4 gap-1 pt-1">
                {[
                  { label: "0x Flat", val: 0.0 },
                  { label: "0.6x Low", val: 0.6 },
                  { label: "1.2x Nat", val: 1.25 },
                  { label: "2.4x High", val: 2.4 },
                ].map((s) => (
                  <button
                    key={s.val}
                    onClick={() => setDepthScale(s.val)}
                    className={`py-1 rounded-lg text-[10px] font-semibold border transition ${
                      Math.abs(depthScale - s.val) < 0.1
                        ? "bg-blue-600 text-white border-blue-400"
                        : "bg-slate-800/80 text-slate-400 border-slate-700 hover:text-white"
                    }`}
                  >
                    {s.label}
                  </button>
                ))}
              </div>
            </div>

            {/* 2. Visual Render Mode Selection */}
            <div className="space-y-2">
              <span className="text-[11px] font-bold uppercase tracking-wider text-slate-400">
                Visualization Mode
              </span>
              <div className="grid grid-cols-2 gap-1.5">
                {[
                  { id: "textured", label: "🛰️ Textured 3D", desc: "Satellite Orthophoto" },
                  { id: "heatmap", label: "🌡️ Depth Heatmap", desc: "Turbo Elevation Ramp" },
                  { id: "wireframe", label: "🕸️ 3D Mesh", desc: "16k Vertex Polygons" },
                  { id: "clay", label: "🏛️ Shaded Clay", desc: "Architectural Relief" },
                  { id: "hybrid", label: "🔮 Hybrid X-Ray", desc: "Photo + Cyan Mesh" },
                ].map((m) => (
                  <button
                    key={m.id}
                    onClick={() => setRenderMode(m.id as Deep3DRenderMode)}
                    className={`p-2 rounded-xl border text-left transition ${
                      renderMode === m.id
                        ? "border-blue-500 bg-blue-500/20 text-white font-semibold"
                        : "border-slate-800 bg-slate-950/40 text-slate-400 hover:bg-slate-800 hover:text-slate-200"
                    }`}
                  >
                    <div className="text-xs font-semibold">{m.label}</div>
                    <div className="text-[10px] text-slate-400">{m.desc}</div>
                  </button>
                ))}
              </div>
            </div>

            {/* 3. Cadastral Roof Typology & Priors */}
            <div className="space-y-2 pt-2 border-t border-slate-800">
              <span className="text-[11px] font-bold uppercase tracking-wider text-slate-400">
                Architectural Roof Typology
              </span>
              <div className="grid grid-cols-4 gap-1">
                {([
                  { id: "hipped", label: "Hipped", icon: "🏠" },
                  { id: "gabled", label: "Gabled", icon: "🏡" },
                  { id: "flat", label: "Flat", icon: "🏗️" },
                  { id: "executive", label: "Executive", icon: "🏢" },
                ] as const).map((r) => (
                  <button
                    key={r.id}
                    onClick={() => {
                      setRoofType(r.id);
                      runModel(activeLat, activeLon, r.id, eaveHeight, ridgeHeight, vintage, activeBuilding);
                    }}
                    className={`p-2 rounded-xl border text-center transition ${
                      roofType === r.id
                        ? "border-blue-500 bg-blue-600 text-white font-semibold"
                        : "border-slate-800 bg-slate-950/40 text-slate-400 hover:bg-slate-800"
                    }`}
                  >
                    <div className="text-base">{r.icon}</div>
                    <div className="text-[11px] font-medium">{r.label}</div>
                  </button>
                ))}
              </div>
            </div>

            {/* 4. Height Tuning Parameters */}
            <div className="space-y-3 pt-2 border-t border-slate-800">
              <div>
                <div className="flex justify-between text-xs mb-1">
                  <span className="text-slate-400">Eave Wall Height</span>
                  <span className="font-mono font-bold text-slate-200">{eaveHeight.toFixed(1)} m</span>
                </div>
                <input
                  type="range"
                  min="4.0"
                  max="12.0"
                  step="0.5"
                  value={eaveHeight}
                  onChange={(e) => {
                    const v = parseFloat(e.target.value);
                    setEaveHeight(v);
                    runModel(activeLat, activeLon, roofType, v, ridgeHeight, vintage, activeBuilding);
                  }}
                  className="w-full h-1.5 bg-slate-800 rounded-lg appearance-none cursor-pointer accent-blue-500"
                />
              </div>

              <div>
                <div className="flex justify-between text-xs mb-1">
                  <span className="text-slate-400">Ridge Peak Height</span>
                  <span className="font-mono font-bold text-slate-200">{ridgeHeight.toFixed(1)} m</span>
                </div>
                <input
                  type="range"
                  min="10.0"
                  max="25.0"
                  step="0.5"
                  value={ridgeHeight}
                  onChange={(e) => {
                    const v = parseFloat(e.target.value);
                    setRidgeHeight(v);
                    runModel(activeLat, activeLon, roofType, eaveHeight, v, vintage, activeBuilding);
                  }}
                  className="w-full h-1.5 bg-slate-800 rounded-lg appearance-none cursor-pointer accent-blue-500"
                />
              </div>
            </div>

            {/* 5. Satellite Vintage Selector */}
            <div className="space-y-2 pt-2 border-t border-slate-800">
              <span className="text-[11px] font-bold uppercase tracking-wider text-slate-400">
                Satellite Imagery Vintage
              </span>
              <div className="grid grid-cols-2 gap-1.5 text-xs">
                <button
                  onClick={() => {
                    setVintage("google-latest");
                    runModel(activeLat, activeLon, roofType, eaveHeight, ridgeHeight, "google-latest", activeBuilding);
                  }}
                  className={`p-2 rounded-xl border text-left transition ${
                    vintage === "google-latest"
                      ? "border-blue-500 bg-blue-600/20 text-white font-semibold"
                      : "border-slate-800 bg-slate-950/40 text-slate-400 hover:bg-slate-800"
                  }`}
                >
                  <div>✈️ Google Ortho</div>
                  <div className="text-[10px] text-slate-400">~2022 Ultra-HD</div>
                </button>
                <button
                  onClick={() => {
                    setVintage("esri-recent");
                    runModel(activeLat, activeLon, roofType, eaveHeight, ridgeHeight, "esri-recent", activeBuilding);
                  }}
                  className={`p-2 rounded-xl border text-left transition ${
                    vintage === "esri-recent"
                      ? "border-blue-500 bg-blue-600/20 text-white font-semibold"
                      : "border-slate-800 bg-slate-950/40 text-slate-400 hover:bg-slate-800"
                  }`}
                >
                  <div>🛰️ ESRI Orbital</div>
                  <div className="text-[10px] text-slate-400">~2024 Recent</div>
                </button>
              </div>
            </div>
          </div>

          {/* Right Floating Architectural Metrics & Profile Drawer */}
          <div className="absolute top-4 right-4 z-20 w-72 max-h-[calc(100%-2rem)] overflow-y-auto rounded-2xl bg-slate-900/95 backdrop-blur-xl border border-slate-700/80 p-4 shadow-2xl space-y-4 text-slate-200">
            <div className="flex items-center space-x-2 text-xs font-bold uppercase tracking-wider text-emerald-400">
              <Flame className="w-3.5 h-3.5 text-emerald-400" />
              <span>Morphological Metrics</span>
            </div>

            {/* Metrics Grid */}
            <div className="grid grid-cols-2 gap-2 text-xs">
              <div className="p-2.5 rounded-xl bg-slate-950/60 border border-slate-800">
                <span className="text-[10px] text-slate-400 uppercase">Ridge Peak (Zmax)</span>
                <div className="text-base font-bold font-mono text-emerald-400 mt-0.5">
                  {modelOutput?.metrics.peakHeightMeters ?? 16.5} m
                </div>
              </div>

              <div className="p-2.5 rounded-xl bg-slate-950/60 border border-slate-800">
                <span className="text-[10px] text-slate-400 uppercase">Eave Height</span>
                <div className="text-base font-bold font-mono text-blue-400 mt-0.5">
                  {modelOutput?.metrics.eaveHeightMeters ?? 7.5} m
                </div>
              </div>

              <div className="p-2.5 rounded-xl bg-slate-950/60 border border-slate-800">
                <span className="text-[10px] text-slate-400 uppercase">Extracted Roof Area</span>
                <div className="text-base font-bold font-mono text-cyan-400 mt-0.5">
                  {modelOutput?.metrics.roofAreaM2 ?? 0} m²
                </div>
              </div>

              <div className="p-2.5 rounded-xl bg-slate-950/60 border border-slate-800">
                <span className="text-[10px] text-slate-400 uppercase">Est. Roof Pitch</span>
                <div className="text-base font-bold font-mono text-amber-400 mt-0.5">
                  {modelOutput?.metrics.roofPitchDegrees ?? 26}°
                </div>
              </div>

              <div className="p-2.5 rounded-xl bg-slate-950/60 border border-slate-800">
                <span className="text-[10px] text-slate-400 uppercase">Tree Canopy</span>
                <div className="text-base font-bold font-mono text-emerald-300 mt-0.5">
                  {modelOutput?.metrics.canopyHeightMeters ?? 10.8} m
                </div>
              </div>
            </div>

            {/* Enclosed Architectural Volume */}
            <div className="p-3 rounded-xl bg-slate-950/60 border border-slate-800 space-y-1">
              <div className="flex justify-between items-center text-xs">
                <span className="text-slate-400">Enclosed Building Volume</span>
                <span className="font-mono font-bold text-white">
                  {(modelOutput?.metrics.estimatedVolumeM3 ?? 3312).toLocaleString()} m³
                </span>
              </div>
              <div className="flex justify-between items-center text-[11px] text-slate-400">
                <span>Active 3D Grid</span>
                <span className="font-mono">128 × 128 (16,384 Vertices)</span>
              </div>
              <div className="flex justify-between items-center text-[11px] text-slate-400">
                <span>Triangular Polygons</span>
                <span className="font-mono">32,258 Triangles</span>
              </div>
            </div>

            {/* Interactive North-South Elevation Profile (SVG) */}
            <div className="p-3 rounded-xl bg-slate-950/60 border border-slate-800 space-y-2">
              <div className="flex items-center justify-between text-xs">
                <span className="font-bold text-slate-300">Elevation Cross-Section</span>
                <span className="text-[10px] font-mono text-blue-400">South ➔ North</span>
              </div>

              <div className="h-20 w-full bg-slate-900 rounded-lg p-1 relative border border-slate-800/80 flex items-end">
                {crossSectionData.length > 0 && (
                  <svg className="w-full h-full overflow-visible" viewBox="0 0 64 25" preserveAspectRatio="none">
                    <defs>
                      <linearGradient id="profileGrad" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stopColor="#38bdf8" stopOpacity="0.8" />
                        <stop offset="100%" stopColor="#0284c7" stopOpacity="0.1" />
                      </linearGradient>
                    </defs>
                    <polygon
                      points={`0,25 ${crossSectionData
                        .map((h, i) => `${i},${Math.max(0, 25 - h * 1.1)}`)
                        .join(" ")} 64,25`}
                      fill="url(#profileGrad)"
                    />
                    <polyline
                      points={crossSectionData
                        .map((h, i) => `${i},${Math.max(0, 25 - h * 1.1)}`)
                        .join(" ")}
                      fill="none"
                      stroke="#38bdf8"
                      strokeWidth="1.2"
                    />
                  </svg>
                )}
              </div>
              <div className="flex justify-between text-[10px] text-slate-400 font-mono">
                <span>Front Yard</span>
                <span>Ridge Peak</span>
                <span>Backyard Gum</span>
              </div>
            </div>

            {/* Depth Heatmap Legend */}
            <div className="p-3 rounded-xl bg-slate-950/60 border border-slate-800 space-y-1.5">
              <span className="text-[10px] font-bold uppercase tracking-wider text-slate-400">
                Height Heatmap Ramp
              </span>
              <div className="h-3 w-full rounded-md bg-gradient-to-r from-blue-900 via-emerald-500 via-amber-500 to-white border border-slate-700/50" />
              <div className="flex justify-between text-[9px] text-slate-400 font-mono">
                <span>0m Ground</span>
                <span>8m Canopy</span>
                <span>12m Eave</span>
                <span>18m Peak</span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
