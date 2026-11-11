# Drone capture at 27 Cambridge Road, Mooroolbark: rules, kit, protocol (checked 28 September 2026)

Purpose: nadir and oblique imagery plus video of the anchor property's roof, sheds, carport and trees as a
teacher source for the roof pipeline (`teacher_sources.py` source `drone-ortho` takes the OpenDroneMap orthomosaic).
Facts below were verified on the linked pages on 28 September 2026 by a research agent; computed values are marked.

## 1. The legal position first

Under the rules in force today, a business-purpose flight of a consumer drone over this suburban lot does not fit
CASA's standard operating conditions, whatever the drone weighs. The pipeline purpose makes the flight business use
(CASA counts "research and development" and anything "not for sport or recreation" as business use).

- Micro (250 g or less) and excluded (250 g to 2 kg) operators need an ARN, free operator accreditation and drone
  registration, and must stay inside the standard operating conditions: visual line of sight, 120 m or lower, by
  day, never over people, not within 30 m of a person not part of the operation, not over a populous area, one
  drone at a time. Sources: https://www.casa.gov.au/drones/operator-accreditation-certificate/drone-weight-categories-and-requirements
  and the Part 101 Plain English Guide (p.7).
- CASA's populous-area guidance names "residential areas" and says to keep 30 m from "anywhere a person could be,
  such as in a house or a car" (https://www.casa.gov.au/drones/drone-rules/flying-populous-areas). The lot is about
  50 m by 47 m (Vicmap parcel), so any point over it is within 30 m of a neighbouring dwelling (computed).
- Micro and excluded operators cannot apply for approvals outside those conditions (CASA PP 2609US, p.9). The
  lawful route today is a Remote Pilot Licence plus a Remote Operator's Certificate plus an approval to fly near
  people (https://www.casa.gov.au/drones/flight-authorisations/flying-over-and-near-people), or hiring an operator
  who holds them.
- What may change: PP 2609US (consultation closed 31 May 2026, summary published 13 August 2026, nothing in force)
  proposes that drones up to 25 kg could fly near people who are sheltered by a structure "such as buildings".
  That would cover neighbours indoors. Until it is law, the constraint stands.
- Penalties: infringement notices up to A$1,820 per offence, court fines up to A$18,200
  (https://www.casa.gov.au/knowyourdrone/drone-rules).

Everything below is therefore written for two cases: a hired ReOC operator with the approval, or the user after
obtaining the credentials described in `drone-licensing-reference` (memory) and CP12.

## 2. Airspace at the site

| site | coordinates used | distance (km, computed) |
|---|---|---|
| Lilydale YLIL, non-controlled, CTAF 119.1, parachuting east of runway | -37.69194, 145.36639 (ERSA ARP) | 11.92 |
| Coldstream YCEM, non-controlled | -37.72833, 145.40889 (ERSA ARP) | 10.92 |
| Maroondah Hospital (no helipad) | -37.80667, 145.25528 | 5.35 |
| Olinda helibase AU-0111 | -37.85940, 145.37660 | 9.52 |
| Moorabbin YMMB, nearest controlled aerodrome | -37.97777, 145.09980 | 28.0 |

No aerodrome inside 5.5 km, no helicopter landing site inside 1.4 km, not in a control zone. Not checked: the
Melbourne CTA base over the site, temporary restricted areas (NOTAMs), and live output of a CASA-verified app
(ok2fly, OpenSky, AVCRM). Check an app before each flight; the apps do not assess the populous-area rule.

## 3. Privacy

- Surveillance Devices Act 1999 (Vic), s.3: activity "outside a building" is not a "private activity", so filming
  roofs, yards and sheds is outside the s.7 offence. Filming into windows could engage it; publication is
  controlled by s.11.
- Privacy Act 1988 (Cth) applies to organisations above A$3M turnover or any business trading in personal
  information. The statutory tort for serious invasions of privacy (in force 10 June 2025) applies to individuals.
- Yarra Ranges Neighbourhood Amenity Local Law cl.5.4 needs a film permit for commercial filming on roads, council
  land or public places; not for take-off and filming from your own lot.

## 4. Kit

| drone | weight | camera | mechanical shutter | third-party mapping apps | grid missions |
|---|---|---|---|---|---|
| DJI Mini 4 Pro (RC-N2), A$949 | under 249 g | 1/1.3" 48 MP, 24 mm eq | no | yes: DJI Mobile SDK 5 with an Android phone (Dronelink, Litchi Pilot, DroneDeploy, Map Pilot Pro Android) | native |
| DJI Mini 5 Pro (RC-N3), A$1,119 | 249.9 g | 1" 50 MP | no | no SDK (KMZ export into DJI Fly only) | workaround |
| DJI Air 3S, A$1,699 | 724 g | 1" 50 MP + 70 mm | no | no SDK | workaround |
| DJI Mavic 3 Enterprise, about A$6,050 to 6,320 | 1,050 g | 4/3" 20 MP | yes | DJI Pilot 2 mapping and oblique modes | native |

Recommendation: Mini 4 Pro with the RC-N2 controller and an Android phone (not the RC 2 combo, which the mapping
apps cannot drive), two spare batteries (A$99 each), charging hub (A$69), ND16/64/256 set (A$79). Registration is
free at 500 g or less. Dronelink's hobbyist plan excludes business use. Prices from store.dji.com/au on
28 September 2026.

## 5. Ground sample distance (computed from DJI's FOV and pixel counts)

| AGL | Mini 4 Pro 12 MP | Mini 4 Pro 48 MP | footprint 12 MP (m) | Mavic 3E 20 MP |
|---|---|---|---|---|
| 30 m | 1.04 cm | 0.52 cm | 41.8 x 31.3 | 0.82 cm |
| 40 m | 1.38 cm | 0.69 cm | 55.7 x 41.8 | 1.09 cm |
| 60 m | 2.07 cm | 1.04 cm | 83.6 x 62.7 | 1.64 cm |
| 120 m | 4.15 cm | 2.07 cm | 167.2 x 125.4 | 3.28 cm |

Use 12 MP while moving (2 s interval, 21 ms rolling-shutter readout, about 6 px skew at 3 m/s) and 48 MP only
when hovering (58 ms readout, 5 s minimum interval). Shutter 1/1000 s keeps motion blur under 0.6 px.

## 6. Capture protocol (for the permitted case)

- Nadir grid at 30 m AGL, 75/75 overlap: line spacing 10.4 m, photo spacing 7.8 m, about 48 images over the lot,
  72 at 80/80; a 40 m pass with a 10 m buffer adds 56. Fly 2 m/s or hover-and-shoot. ODM asks for 3 to 4 times
  the tallest feature's height, so add a 60 m context pass (20 to 30 images) if trees are tall.
- Orbits for roof faces and 3D: ring 1 radius 20 m, 20 m above the ridge, gimbal -45 degrees, a photo every
  8 degrees (45 images); ring 2 radius 15 m, 26 m above the ridge, -60 degrees (45 images); ten transition frames
  near -75 degrees.
- Neighbouring roof faces from own airspace (only within the legal case above): perimeter pass 2 to 3 m inside the
  boundary at 30 m, camera outward at -45 degrees, about 11 images per side. Avoid windows.
- Total about 190 to 230 stills, inside ODM's 16 GB tier for 250 images.
- Light: overcast is the diffuser; otherwise 10:00 to 14:00 solar time. Solar noon 28 September is 12:09 AEST at
  53.9 degrees elevation (shadow 0.73 of height); 21 December 13:16 AEDT at 75.6 degrees (0.26). A second sunny
  capture gives shadow cues for heights.
- Video for splatting or NeRF: 4K at 30 or 60 fps, H.265, ISO 100 fixed, white balance and exposure locked,
  shutter 1/500 to 1/1000 s (ND16 in full sun, none under overcast), same two rings at 1 to 1.5 m/s, extract 2 to
  3 fps and drop blurry frames by Laplacian variance. Timed 12 MP stills every 2 s are the higher-quality
  alternative.

## 7. Processing on this Mac

- OpenDroneMap and WebODM publish linux/arm64 images (opendronemap/odm 3.6.2, nodeodm, webodm_webapp 3.1.4);
  GPU tags are amd64 only, so processing is CPU. Raise Docker's memory to 64 GB or more (machine has 128 GB).
- Single-lot command (flags documented at https://docs.opendronemap.org/arguments/, the combination is a judgement):

```
docker run --rm -ti -v "$HOME/odm:/datasets" opendronemap/odm:3.6.2 --project-path /datasets cambridge27 \
  --dsm --dtm --pc-classify --orthophoto-resolution 1.0 --dem-resolution 2.0 \
  --feature-quality ultra --min-num-features 16000 --pc-quality high \
  --mesh-octree-depth 12 --mesh-size 600000 --matcher-neighbors 0 --matcher-type flann \
  --rolling-shutter --boundary /datasets/cambridge27/lot_buffer.geojson --crop 1 \
  --smrf-window 20 --cog --gltf --3d-tiles --max-concurrency 12
```

- Estimated run time (not measured): 30 to 60 minutes for about 200 12 MP images at pc-quality high; 1.5 to 3 h
  at ultra.
- Georeferencing without RTK: at least five ground control points seen in three photos each (`gcp_list.txt`);
  Victorian survey marks (SMES via LASSI) give GDA2020 and AHD values; Vicmap parcel corners are only 0.5 m class
  (coarse horizontal alignment); `--align` to an open DEM is experimental and Victorian LiDAR over Mooroolbark is
  not open.
- Ingest: `task eyes:teacher-sources -- register --source drone-ortho --path odm_orthophoto.tif --capture <date>
  --place "27 Cambridge Road"`, then `select`, `crop`, and `task training:teacher -- calibrate|teach|finalize|measure
  --source drone-ortho`. Limit today: `select` needs a raster at least two 272 m windows wide, so a single-lot
  orthomosaic stops with a clear message; a lot-sized window mode is the next piece of the teacher loop.

## Not verified

Melbourne CTA base; NOTAM restricted areas; live safety-app output; Part 18 of the Yarra Ranges local law;
Dronelink Professional, DroneDeploy and Map Pilot Pro pricing; ODM run times on this Mac; Google tile terms for use
as ground control.
