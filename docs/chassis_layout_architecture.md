# Chassis-Based Factory Layout Architecture & Specification

## 1. Executive Summary & The Chassis Concept

This document specifies the **Chassis-Based Factory Layout Architecture** for Factorilog.

### 1.1 The Chassis Abstraction
A **chassis** is a pre-engineered physical template that provides:
1. **Zoned Production Cores**: Bounded physical sites for machine clusters.
2. **Fixed Infrastructure**: Pre-routed Power Distribution Networks (PDN), radar coverage, and surface paving.
3. **Dedicated Transport Channels**: Standardized streets that isolate inter-block transit from local machine connections.

Rather than treating the factory floor as a single unbounded plane, the chassis partitions the factory into an orderly grid.

```
+-------------------+-------------------+
|                   |                   |
|   City Block      |   City Block      |
|      (0, 1)       |      (1, 1)       |
|   [94x94 Core]    |   [94x94 Core]    |
|                   |                   |
+=== 6-Tile Road ===#=== 6-Tile Road ===+
|                   |                   |
|   City Block      |   City Block      |
|      (0, 0)       |      (1, 0)       |
|   [94x94 Core]    |   [94x94 Core]    |
|                   |                   |
+-------------------+-------------------+
```

### 1.2 Layout Typology
The chassis architecture supports multiple layout topologies:
- **Type 1: Orthogonal City Block (Square Grid)** -- Primary reference implementation.
- **Type 2: Hexagonal Lattice (Honeycomb Grid)** -- Three-axis isometric layout.
- **Type 3: Linear Spine and Rib** -- Central highway with perpendicular industrial branches.

This document defines the interface and physical implementation for **Type 1: Orthogonal City Block**.

---

## 2. Type 1: Orthogonal City Block Physical Specification

### 2.1 Geometric Parameters
The square city block template derives from the reference blueprint [`cityblock.bp`](../cityblock.bp).

| Parameter | Value | Description |
|:---|:---|:---|
| **Repeat Pitch ($P_x, P_y$)** | $100 \times 100$ tiles | Distance between identical block origins |
| **Total Block Footprint** | $116 \times 116$ tiles | Envelope including perimeter half-streets and overlaps |
| **Channel Width ($W_{\text{road}}$)** | 6 tiles | Width of straight inter-block transport channels |
| **Intersection Footprint** | $12 \times 12$ tiles | Expanded junction at 4-way crossroads |
| **Core Usable Area ($C_x, C_y$)** | **$94 \times 94$ tiles** | Interior space reserved for production machines |
| **Core Area Budget** | **8,836 tiles** | Total area available per production block |

### 2.2 Core Footprint Capacity
A single $94 \times 94$ tile core provides large capacity. It accommodates:
- Up to **120 assembling machines** ($5 \times 3$ standard cells including inserters).
- Up to **150 electric furnaces** with full inserter channels.
- Approximately **70 chemical plants** with dedicated fluid manifolds.

Most recipe stages fit completely within a single city block core.

### 2.3 Pre-Routed Infrastructure
The chassis blueprint supplies utility infrastructure before machine placement:
1. **Power Grid**:
   - Big electric poles sit at coordinates with 30-tile spacing along roads.
   - Big electric poles provide a $30 \times 30$ tile wire reach and $4 \times 4$ tile supply area.
   - Sub-stations or medium poles inside the core connect to the perimeter grid.
2. **Surveillance and Radar**:
   - Radar units sit at block corner intersections to give continuous map visibility.
3. **Pavement and Ground Speed**:
   - Refined concrete paves the road surface (140% player movement speed).
   - Road boundaries use `hazard-concrete-left` markings to demarcate the core threshold.

---

## 3. Channel & Router Intersection Architecture

### 3.1 Road Channel Structure (6-Tile Street)
Each 6-tile street between adjacent city blocks serves as a high-density transit corridor:

```
Street Cross-Section (6 Tiles Wide):
+--------+--------+--------+--------+--------+--------+
| Tile 0 | Tile 1 | Tile 2 | Tile 3 | Tile 4 | Tile 5 |
| Track0 | Track1 | Track2 | Track3 | Track4 | Track5 |
+--------+--------+--------+--------+--------+--------+
|<----- Direction A ------>|<----- Direction B ------>|
```

- **Surface Capacity**: 6 parallel surface belts or pipes per road.
- **Multilayer Capacity (Belt Weaving)**:
  - Yellow underground belts (max span 4).
  - Red underground belts (max span 6).
  - Blue underground belts (max span 8).
  - By weaving underground belts of different tiers, a single tile lane can carry up to 3 independent item nets.
  - A 6-tile corridor can physically accommodate up to **18 item streams**.

### 3.2 4-Way Router Intersection (Switchbox)
The 4-way intersection operates as a passive switchbox:

```
               North Channel (M2)
                    |  |  |
                    v  ^  |
+-------------------+--+--+-------------------+
| Block (0, 1)      |  |  |      Block (1, 1) |
+-------------------+--+--+-------------------+
| West     ===> ===>|  |  |===> ===>     East |
| Channel  <=== <===|  |  |<=== <===  Channel |
| (M1)              |  |  |              (M1) |
+-------------------+--+--+-------------------+
| Block (0, 0)      |  |  |      Block (1, 0) |
+-------------------+--+--+-------------------+
                    |  |  |
                    v  ^  |
               South Channel (M2)
```

1. **Straight-Through Orthogonal Crossings**:
   - **Horizontal (East-West)** traffic dives underground before the intersection ($12$-tile crossing) and surfaces on the opposite side.
   - **Vertical (North-South)** traffic traverses the intersection on surface belts.
   - Underground pipes cross the intersection with standard pipe-to-ground pairs (max underground span is 10 tiles, clearing 6-to-10 tile gaps).
2. **Lane Switching and Directional Turns**:
   - Standard 2x2 splitters positioned at intersection approaches switch items between lanes.
   - Filter splitters permit specific item signals to peel off into a 90-degree turn toward an adjacent channel.
   - Items require zero Lua scripts or creative mode blocks. All routing uses vanilla mechanics.

### 3.3 Block Ingress & Egress (Connection Blocks)
- **Ingress Ports**: Tap into street channels on the West or South face of the block.
- **Egress Ports**: Inject produced goods into the street channels on the East or North face.
- Inserters and splitters bridge the boundary between the 6-tile road and the $94 \times 94$ core.

---

## 4. Hierarchical Synthesis & EDA Integration

### 4.1 Two-Level Hierarchical Flow
Factorilog implements a two-level physical design hierarchy:

```
[System Recipe Graph]
         |
         v
+-------------------------------------------------+
| Level 1: Macro Floorplanner                     |
| - Partitions recipes into discrete City Blocks  |
| - Assigns Block (X, Y) coordinates              |
| - Routes inter-block buses via road channels    |
+-------------------------------------------------+
         |
         +-----------------------+
         |                       |
         v                       v
+-------------------+   +-------------------+
| Level 2: Block 0  |   | Level 2: Block 1  |
| - OpenROAD PnR    |   | - OpenROAD PnR    |
| - Standard Cells  |   | - Standard Cells  |
| - 94x94 Core Area |   | - 94x94 Core Area |
+-------------------+   +-------------------+
```

### 4.2 Level 1: Macro Floorplanner
1. **Recipe Clustering**:
   - Groups related assembly recipes into single-block units.
   - *Example*: Advanced Circuits, Electronic Circuits, and Plastic Bar processing clustered in adjacent blocks.
2. **Block Allocation**:
   - Maps each recipe cluster to a grid site: $\text{Block}(x, y)$.
   - When a cluster exceeds the 8,836 tile area budget, the floorplanner allocates multiple blocks.
3. **Channel Bus Allocation**:
   - Reserves highway lanes in the 6-tile streets for global nets.

### 4.3 Level 2: OpenROAD Block PnR
Within each $94 \times 94$ core:
1. **Site Grid**: Defines `SITE tile` rows from $(x=3, y=3)$ to $(x=97, y=97)$.
2. **Perimeter Blockage**: Marks coordinates outside the $94 \times 94$ core as `PLACEMENT BLOCKAGE` so machines do not spill into streets.
3. **Pin Placement**: Locates block I/O pins along the perimeter tiles facing the road channels.
4. **Local Detail Routing**: Routes machines to local inserter slots and perimeter pins.

---

## 5. Extensibility: Generalized Chassis Interface

To support future factory topologies, the chassis abstraction defines a standard interface:

```python
class ChassisTemplate(ABC):
    """Abstract interface for structured factory chassis templates."""

    @abstractmethod
    def get_pitch(self) -> tuple[int, int]:
        """Return (pitch_x, pitch_y) in tiles between repeating grid sites."""
        pass

    @abstractmethod
    def get_core_bounds(self, block_x: int, block_y: int) -> tuple[int, int, int, int]:
        """Return (min_x, min_y, width, height) of the usable machine core."""
        pass

    @abstractmethod
    def get_channel_lanes(self, segment: RoadSegment) -> list[TrackReservation]:
        """Return available routing tracks along a highway channel."""
        pass

    @abstractmethod
    def emit_base_blueprint(self, grid_w: int, grid_h: int) -> dict[str, Any]:
        """Emit base entities (poles, radars, pavement) for an N x M chassis grid."""
        pass
```

### 5.1 Alternative Topologies Under Consideration
1. **Type 2: Hexagonal Chassis**:
   - Uses 3-axis coordinate systems $(q, r, s)$.
   - Roads meet at 120-degree 3-way Y-junctions.
   - Y-junctions reduce crossing conflicts compared to 4-way intersections.
2. **Type 3: Linear Spine (Main Bus Chassis)**:
   - Central highway of width 16-32 tiles.
   - Perpendicular factory ribs extend North and South.
   - Best suited for progression stages before complete rail or grid logistics.
