import * as THREE from "three";
import type { Address } from "./geo-engine";

export interface HouseOptions {
  width?: number; // meters along X
  depth?: number; // meters along Y
  wallHeight?: number; // eave height in meters
  roofHeight?: number; // roof rise in meters
  roofType?: "hipped" | "gabled" | "executive";
  roofColor?: number;
  wallColor?: number;
  isTarget?: boolean;
  addressTitle?: string;
  rotation?: number; // radians
}

export interface TreeOptions {
  type?: "eucalyptus" | "flowering_gum" | "canopy_shade";
  height?: number;
  canopyRadius?: number;
}

/**
 * Creates an authentic LoD2 Hipped Roof geometry with precise polygonal facets,
 * eaves overhang, and vertex normals for realistic sunlight reflectance.
 */
function createHippedRoofGeometry(
  width: number,
  depth: number,
  roofRise: number,
  overhang = 0.45
): THREE.BufferGeometry {
  const x0 = -width / 2 - overhang;
  const x1 = width / 2 + overhang;
  const y0 = -depth / 2 - overhang;
  const y1 = depth / 2 + overhang;
  const zEave = 0;
  const zRidge = roofRise;

  // Australian suburban hipped roof ridge length
  const ridgeLen = Math.max(width * 0.35, width - depth * 0.65);
  const rx0 = -ridgeLen / 2;
  const rx1 = ridgeLen / 2;
  const ry = 0;

  const positions: number[] = [
    // 1. South Facet (Trapezoid: 2 Triangles)
    x0, y0, zEave,
    x1, y0, zEave,
    rx1, ry, zRidge,

    x0, y0, zEave,
    rx1, ry, zRidge,
    rx0, ry, zRidge,

    // 2. North Facet (Trapezoid: 2 Triangles)
    x1, y1, zEave,
    x0, y1, zEave,
    rx0, ry, zRidge,

    x1, y1, zEave,
    rx0, ry, zRidge,
    rx1, ry, zRidge,

    // 3. East Facet (Hip End Triangle)
    x1, y0, zEave,
    x1, y1, zEave,
    rx1, ry, zRidge,

    // 4. West Facet (Hip End Triangle)
    x0, y1, zEave,
    x0, y0, zEave,
    rx0, ry, zRidge,
  ];

  const geom = new THREE.BufferGeometry();
  geom.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
  geom.computeVertexNormals();
  return geom;
}

/**
 * Creates an authentic LoD2 Gabled Roof geometry with triangular gable walls.
 */
function createGabledRoofGeometry(
  width: number,
  depth: number,
  roofRise: number,
  overhang = 0.45
): { roofGeom: THREE.BufferGeometry; gableGeom: THREE.BufferGeometry } {
  const x0 = -width / 2 - overhang;
  const x1 = width / 2 + overhang;
  const y0 = -depth / 2 - overhang;
  const y1 = depth / 2 + overhang;
  const zEave = 0;
  const zRidge = roofRise;

  const roofPositions: number[] = [
    // South Sloping Plane
    x0, y0, zEave,
    x1, y0, zEave,
    x1, 0, zRidge,

    x0, y0, zEave,
    x1, 0, zRidge,
    x0, 0, zRidge,

    // North Sloping Plane
    x1, y1, zEave,
    x0, y1, zEave,
    x0, 0, zRidge,

    x1, y1, zEave,
    x0, 0, zRidge,
    x1, 0, zRidge,
  ];

  const gablePositions: number[] = [
    // West Gable Wall
    -width / 2, -depth / 2, 0,
    -width / 2, 0, roofRise,
    -width / 2, depth / 2, 0,

    // East Gable Wall
    width / 2, depth / 2, 0,
    width / 2, 0, roofRise,
    width / 2, -depth / 2, 0,
  ];

  const roofGeom = new THREE.BufferGeometry();
  roofGeom.setAttribute("position", new THREE.Float32BufferAttribute(roofPositions, 3));
  roofGeom.computeVertexNormals();

  const gableGeom = new THREE.BufferGeometry();
  gableGeom.setAttribute("position", new THREE.Float32BufferAttribute(gablePositions, 3));
  gableGeom.computeVertexNormals();

  return { roofGeom, gableGeom };
}

/**
 * Creates a complete LoD2 3D House Mesh (Walls, Pitched Roof, Solar Panels, Driveway).
 */
export function createLod2HouseGroup(opts: HouseOptions): THREE.Group {
  const group = new THREE.Group();

  const width = opts.width ?? (opts.isTarget ? 15.5 : 13.5);
  const depth = opts.depth ?? (opts.isTarget ? 16.0 : 14.5);
  const wallHeight = opts.wallHeight ?? (opts.isTarget ? 6.2 : 3.2);
  const roofRise = opts.roofHeight ?? (opts.isTarget ? 3.0 : 2.5);
  const isTarget = opts.isTarget ?? false;

  // Material palette (Terracotta, Monument Slate, Australian Brick/Render)
  const wallHex = opts.wallColor ?? (isTarget ? 0xf4f1eb : 0xd89f7f);
  const roofHex = opts.roofColor ?? (isTarget ? 0x24282f : 0xaa4c33);

  const wallMat = new THREE.MeshStandardMaterial({
    color: wallHex,
    roughness: 0.85,
    metalness: 0.05,
  });

  const roofMat = new THREE.MeshStandardMaterial({
    color: roofHex,
    roughness: 0.65,
    metalness: 0.15,
  });

  // 1. Wall Envelope
  const wallGeom = new THREE.BoxGeometry(width, depth, wallHeight);
  const wallMesh = new THREE.Mesh(wallGeom, wallMat);
  wallMesh.position.set(0, 0, wallHeight / 2);
  wallMesh.castShadow = true;
  wallMesh.receiveShadow = true;
  group.add(wallMesh);

  // 2. Pitched LoD2 Roof
  if (opts.roofType === "gabled") {
    const { roofGeom, gableGeom } = createGabledRoofGeometry(width, depth, roofRise);
    const roofMesh = new THREE.Mesh(roofGeom, roofMat);
    roofMesh.position.set(0, 0, wallHeight);
    group.add(roofMesh);

    const gableMesh = new THREE.Mesh(gableGeom, wallMat);
    gableMesh.position.set(0, 0, wallHeight);
    group.add(gableMesh);
  } else {
    // Default: Australian Hipped Roof
    const roofGeom = createHippedRoofGeometry(width, depth, roofRise);
    const roofMesh = new THREE.Mesh(roofGeom, roofMat);
    roofMesh.position.set(0, 0, wallHeight);
    roofMesh.castShadow = true;
    roofMesh.receiveShadow = true;
    group.add(roofMesh);
  }

  // 3. Rooftop Photovoltaic Solar Panels (North/NW facing roof slope)
  if (isTarget || Math.random() > 0.45) {
    const solarGroup = new THREE.Group();
    const panelMat = new THREE.MeshStandardMaterial({
      color: 0x112238, // Monocrystalline Navy Blue
      metalness: 0.88,
      roughness: 0.18,
    });
    const frameMat = new THREE.MeshStandardMaterial({
      color: 0xc0c5ce, // Aluminum frame
      metalness: 0.9,
      roughness: 0.3,
    });

    const panelCount = isTarget ? 6 : 4;
    for (let i = 0; i < panelCount; i++) {
      const pMesh = new THREE.Mesh(new THREE.BoxGeometry(1.6, 1.0, 0.06), panelMat);
      pMesh.position.set((i - panelCount / 2 + 0.5) * 1.7, depth * 0.22, 0);
      solarGroup.add(pMesh);
    }

    // Tilt solar array to align with north roof slope (~23 degrees)
    solarGroup.rotation.x = 0.40;
    solarGroup.position.set(0, depth * 0.15, wallHeight + roofRise * 0.48);
    group.add(solarGroup);
  }

  // 4. Driveway & Garage Entry
  const drivewayGeom = new THREE.PlaneGeometry(width * 0.42, 6.0);
  const drivewayMat = new THREE.MeshStandardMaterial({
    color: 0x6e7681, // Aggregate concrete
    roughness: 0.9,
  });
  const drivewayMesh = new THREE.Mesh(drivewayGeom, drivewayMat);
  drivewayMesh.position.set(-width * 0.22, -depth / 2 - 3.0, 0.02);
  group.add(drivewayMesh);

  // Garage Door facade panel
  const garageGeom = new THREE.PlaneGeometry(width * 0.36, wallHeight * 0.65);
  const garageMat = new THREE.MeshStandardMaterial({
    color: 0x3b4252,
    roughness: 0.7,
  });
  const garageMesh = new THREE.Mesh(garageGeom, garageMat);
  garageMesh.rotation.x = Math.PI / 2;
  garageMesh.position.set(-width * 0.22, -depth / 2 - 0.04, wallHeight * 0.35);
  group.add(garageMesh);

  // Front Entrance Portico
  const porticoGeom = new THREE.BoxGeometry(2.4, 1.8, 0.25);
  const porticoMat = new THREE.MeshStandardMaterial({ color: 0x24282f });
  const porticoMesh = new THREE.Mesh(porticoGeom, porticoMat);
  porticoMesh.position.set(width * 0.22, -depth / 2 - 0.8, 2.7);
  group.add(porticoMesh);

  // 5. Special Detailing for Searched Address (e.g. 27 Cambridge Rd)
  if (isTarget) {
    // Upper Balcony
    const balconyGeom = new THREE.BoxGeometry(width * 0.5, 2.0, 0.2);
    const balconyMat = new THREE.MeshStandardMaterial({ color: 0x1f232a });
    const balconyMesh = new THREE.Mesh(balconyGeom, balconyMat);
    balconyMesh.position.set(width * 0.15, -depth / 2 - 0.6, wallHeight * 0.52);
    group.add(balconyMesh);

    // Glazed Balustrade
    const balustradeGeom = new THREE.BoxGeometry(width * 0.5, 0.1, 0.95);
    const balustradeMat = new THREE.MeshStandardMaterial({
      color: 0x7090b0,
      roughness: 0.1,
      metalness: 0.7,
      transparent: true,
      opacity: 0.65,
    });
    const balustradeMesh = new THREE.Mesh(balustradeGeom, balustradeMat);
    balustradeMesh.position.set(width * 0.15, -depth / 2 - 1.5, wallHeight * 0.52 + 0.5);
    group.add(balustradeMesh);

    // Ground Target Beacon Ring
    const ringGeom = new THREE.RingGeometry(width * 0.75, width * 0.82, 32);
    const ringMat = new THREE.MeshBasicMaterial({
      color: 0x1a73e8,
      side: THREE.DoubleSide,
      transparent: true,
      opacity: 0.6,
    });
    const ringMesh = new THREE.Mesh(ringGeom, ringMat);
    ringMesh.position.set(0, 0, 0.05);
    group.add(ringMesh);
  }

  // Set lot rotation
  if (opts.rotation) {
    group.rotation.z = opts.rotation;
  }

  return group;
}

/**
 * Creates a realistic Volumetric 3D Tree (Organic trunk, multi-lobed canopy, ground shadow).
 */
export function createProceduralTree(opts?: TreeOptions): THREE.Group {
  const group = new THREE.Group();

  const totalHeight = opts?.height ?? (5.5 + Math.random() * 3.0);
  const canopyRadius = opts?.canopyRadius ?? (2.8 + Math.random() * 1.5);
  const trunkHeight = totalHeight * 0.45;

  // 1. Organic Tapered Trunk
  const trunkGeom = new THREE.CylinderGeometry(0.22, 0.38, trunkHeight, 6);
  const trunkMat = new THREE.MeshStandardMaterial({
    color: 0x4a3b32, // Eucalyptus bark brown
    roughness: 0.95,
  });
  const trunkMesh = new THREE.Mesh(trunkGeom, trunkMat);
  trunkMesh.rotation.x = Math.PI / 2; // Orient along Z axis (up)
  trunkMesh.position.set(0, 0, trunkHeight / 2);
  // Slight organic tilt
  trunkMesh.rotation.z = (Math.random() - 0.5) * 0.15;
  group.add(trunkMesh);

  // 2. Volumetric Multi-Lobed Canopy (Australian Gum / Deciduous Foliage)
  const canopyGroup = new THREE.Group();
  const foliagePalette = [0x3c643e, 0x487349, 0x2e5233, 0x5a7d4a];
  const foliageColor = foliagePalette[Math.floor(Math.random() * foliagePalette.length)];

  const foliageMat = new THREE.MeshStandardMaterial({
    color: foliageColor,
    roughness: 0.8,
    metalness: 0.05,
    flatShading: true,
  });

  // Central foliage sphere
  const mainCanopyGeom = new THREE.IcosahedronGeometry(canopyRadius, 1);
  // Organic jitter to vertices
  const pos = mainCanopyGeom.attributes.position;
  for (let i = 0; i < pos.count; i++) {
    const vx = pos.getX(i);
    const vy = pos.getY(i);
    const vz = pos.getZ(i);
    const factor = 1.0 + (Math.random() - 0.5) * 0.22;
    pos.setXYZ(i, vx * factor, vy * factor, vz * factor);
  }
  mainCanopyGeom.computeVertexNormals();

  const mainCanopyMesh = new THREE.Mesh(mainCanopyGeom, foliageMat);
  mainCanopyMesh.position.set(0, 0, trunkHeight + canopyRadius * 0.75);
  canopyGroup.add(mainCanopyMesh);

  // 2 or 3 overlapping secondary leaf lobes
  const lobeCount = 2 + Math.floor(Math.random() * 2);
  for (let l = 0; l < lobeCount; l++) {
    const lobeRadius = canopyRadius * (0.55 + Math.random() * 0.25);
    const lobeGeom = new THREE.IcosahedronGeometry(lobeRadius, 1);
    const lobeMesh = new THREE.Mesh(lobeGeom, foliageMat);

    const angle = (l / lobeCount) * Math.PI * 2 + Math.random() * 0.5;
    const dist = canopyRadius * 0.45;
    lobeMesh.position.set(
      Math.cos(angle) * dist,
      Math.sin(angle) * dist,
      trunkHeight + canopyRadius * 0.65 + (Math.random() - 0.5) * 0.6
    );
    canopyGroup.add(lobeMesh);
  }

  group.add(canopyGroup);

  // 3. Ground Contact Shadow Disc
  const shadowGeom = new THREE.CircleGeometry(canopyRadius * 0.95, 16);
  const shadowMat = new THREE.MeshBasicMaterial({
    color: 0x11161d,
    transparent: true,
    opacity: 0.28,
  });
  const shadowMesh = new THREE.Mesh(shadowGeom, shadowMat);
  shadowMesh.position.set(0, 0, 0.03);
  group.add(shadowMesh);

  return group;
}
