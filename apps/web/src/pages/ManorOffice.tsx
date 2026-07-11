import { useState, useEffect, useRef, useMemo } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import LoadingSpinner from "../components/ui/LoadingSpinner";

import { t } from "../lib/i18n";
/* ================================================================== *
 *  Manor Office — Phaser 3 pixel-art office with real sprite assets  *
 *  Uses free CC0/CC-BY-SA pixel art from OpenGameArt.org             *
 *  See /public/assets/office/CREDITS.md for full attribution         *
 * ================================================================== */

const GW = 960;
const GH = 540; // 16:9 aspect ratio
const TILE = 32; // LPC standard tile size
const COLS = Math.ceil(GW / TILE);
const ROWS = Math.ceil(GH / TILE);
const BUBBLE_DUR = 3500;

// 12 character sprite sheets (6 male + 6 female), each 64x51 with 4x3 frames (16x17 per frame)
const CHAR_KEYS = [
  "char_m01", "char_m02", "char_m03", "char_m04", "char_m05", "char_m06",
  "char_f01", "char_f02", "char_f03", "char_f04", "char_f05", "char_f06",
];
const CHAR_FRAME_W = 16;
const CHAR_FRAME_H = 17;

const WORK_BUBBLES = [
  t("page.manor_office.work_bubble_01"), t("page.manor_office.work_bubble_02"), t("page.manor_office.work_bubble_03"),
  t("page.manor_office.work_bubble_04"), t("page.manor_office.work_bubble_05"), t("page.manor_office.work_bubble_06"),
  t("page.manor_office.work_bubble_07"), t("page.manor_office.work_bubble_08"), t("page.manor_office.work_bubble_09"),
  t("page.manor_office.work_bubble_10"), t("page.manor_office.work_bubble_11"), t("page.manor_office.work_bubble_12"),
  t("page.manor_office.work_bubble_13"), t("page.manor_office.work_bubble_14"), t("page.manor_office.work_bubble_15"),
  t("page.manor_office.work_bubble_16"), t("page.manor_office.work_bubble_17"), t("page.manor_office.work_bubble_18"),
  t("page.manor_office.work_bubble_19"), t("page.manor_office.work_bubble_20"), t("page.manor_office.work_bubble_21"),
  t("page.manor_office.work_bubble_22"), t("page.manor_office.work_bubble_23"), t("page.manor_office.work_bubble_24"),
  t("page.manor_office.work_bubble_25"), t("page.manor_office.work_bubble_26"), t("page.manor_office.work_bubble_27"),
  t("page.manor_office.work_bubble_28"), t("page.manor_office.work_bubble_29"), t("page.manor_office.work_bubble_30"),
];
const IDLE_BUBBLES = [
  t("page.manor_office.idle_bubble_01"), t("page.manor_office.idle_bubble_02"), t("page.manor_office.idle_bubble_03"),
  t("page.manor_office.idle_bubble_04"), t("page.manor_office.idle_bubble_05"), t("page.manor_office.idle_bubble_06"),
  t("page.manor_office.idle_bubble_07"), t("page.manor_office.idle_bubble_08"), t("page.manor_office.idle_bubble_09"),
  t("page.manor_office.idle_bubble_10"), t("page.manor_office.idle_bubble_11"), t("page.manor_office.idle_bubble_12"),
  t("page.manor_office.idle_bubble_13"), t("page.manor_office.idle_bubble_14"), t("page.manor_office.idle_bubble_15"),
  t("page.manor_office.idle_bubble_16"), t("page.manor_office.idle_bubble_17"), t("page.manor_office.idle_bubble_18"),
  t("page.manor_office.idle_bubble_19"), t("page.manor_office.idle_bubble_20"), t("page.manor_office.idle_bubble_21"),
  t("page.manor_office.idle_bubble_22"), t("page.manor_office.idle_bubble_23"), t("page.manor_office.idle_bubble_24"),
  t("page.manor_office.idle_bubble_25"), t("page.manor_office.idle_bubble_26"), t("page.manor_office.idle_bubble_27"),
];
const CAT_BUBBLES = [
  t("page.manor_office.cat_bubble_01"), t("page.manor_office.cat_bubble_02"), t("page.manor_office.cat_bubble_03"),
  t("page.manor_office.cat_bubble_04"), t("page.manor_office.cat_bubble_05"), t("page.manor_office.cat_bubble_06"),
  t("page.manor_office.cat_bubble_07"), t("page.manor_office.cat_bubble_08"), t("page.manor_office.cat_bubble_09"),
  t("page.manor_office.cat_bubble_10"), t("page.manor_office.cat_bubble_11"), t("page.manor_office.cat_bubble_12"),
  t("page.manor_office.cat_bubble_13"), t("page.manor_office.cat_bubble_14"), t("page.manor_office.cat_bubble_15"),
  t("page.manor_office.cat_bubble_16"), t("page.manor_office.cat_bubble_17"), t("page.manor_office.cat_bubble_18"),
  t("page.manor_office.cat_bubble_19"), t("page.manor_office.cat_bubble_20"), t("page.manor_office.cat_bubble_21"),
];

// Named action destinations for agents
// ── Office Layout System — each workspace gets a unique floor plan ──
type Pt = { x: number; y: number };
interface FurnItem { key: string; x: number; y: number; scale: number; depth?: number; tint?: number }
interface SheetItem { x: number; y: number; sheet: string; sx: number; sy: number; sw: number; sh: number; scale: number; depth: number }
interface OfficeLayout {
  deskSlots: Pt[];
  idleSlots: Pt[];
  wanderPoints: Pt[];
  catWanderPoints: Pt[];
  actionSpots: Record<string, Pt>;
  windows: Pt[];
  ceilingLights: number[];
  floorItems: FurnItem[];
  wallItems: FurnItem[];
  sheetItems: SheetItem[];
}

const OFFICE_LAYOUTS: OfficeLayout[] = [
  { // 0 — Standard Open Office: 2×3 desks, sofa lounge, coffee station
    deskSlots: [
      { x: 160, y: 280 }, { x: 320, y: 280 }, { x: 480, y: 280 },
      { x: 160, y: 410 }, { x: 320, y: 410 }, { x: 480, y: 410 },
    ],
    idleSlots: [
      { x: 700, y: 260 }, { x: 750, y: 300 }, { x: 680, y: 340 },
      { x: 730, y: 220 }, { x: 760, y: 360 }, { x: 700, y: 380 },
    ],
    wanderPoints: [
      { x: 700, y: 260 }, { x: 750, y: 300 }, { x: 680, y: 340 },
      { x: 730, y: 220 }, { x: 760, y: 360 }, { x: 700, y: 380 },
      { x: 650, y: 450 }, { x: 800, y: 280 }, { x: 620, y: 300 },
      { x: 870, y: 350 }, { x: 550, y: 500 }, { x: 400, y: 550 },
    ],
    catWanderPoints: [
      { x: 80, y: 560 }, { x: 200, y: 500 }, { x: 400, y: 530 },
      { x: 600, y: 550 }, { x: 300, y: 450 }, { x: 150, y: 400 },
      { x: 500, y: 480 }, { x: 720, y: 350 }, { x: 870, y: 330 },
    ],
    actionSpots: {
      coffee: { x: 870, y: 310 }, waterCooler: { x: 910, y: 310 }, sofa: { x: 720, y: 240 },
      whiteboard: { x: 260, y: 160 }, server: { x: 920, y: 240 }, door: { x: GW / 2, y: GH - 60 },
      plant1: { x: 40, y: 500 }, plant2: { x: 920, y: 400 }, window: { x: 130, y: 160 },
      snack: { x: 930, y: 380 }, fishTank: { x: 600, y: 200 },
    },
    windows: [{ x: 100, y: 30 }, { x: 440, y: 30 }],
    ceilingLights: [180, 380, 560, 750],
    floorItems: [
      { key: "rug", x: 740, y: 300, scale: 2.0, depth: 1 },
      { key: "sofa", x: 720, y: 215, scale: 1.6 },
      { key: "round_table", x: 720, y: 270, scale: 1.4 },
      { key: "paper_stack", x: 710, y: 265, scale: 1.2 },
      { key: "coffee_cup", x: 730, y: 260, scale: 1.5 },
      { key: "snack_bowl", x: 740, y: 268, scale: 1.3 },
      { key: "coffee", x: 860, y: 290, scale: 1.5 },
      { key: "water_cooler", x: 900, y: 290, scale: 1.5 },
      { key: "vending_machine", x: 930, y: 340, scale: 1.4 },
      { key: "mini_fridge", x: 930, y: 410, scale: 1.5 },
      { key: "server", x: 935, y: 200, scale: 1.3 },
      { key: "fish_tank", x: 600, y: 185, scale: 1.8 },
      { key: "plant", x: 35, y: 510, scale: 2.0 },
      { key: "plant", x: 935, y: 460, scale: 1.6 },
      { key: "plant", x: 600, y: 530, scale: 1.5 },
      { key: "potted_flower", x: 540, y: 530, scale: 1.6 },
      { key: "small_plant", x: 28, y: 200, scale: 1.8 },
      { key: "small_plant", x: 935, y: 155, scale: 1.5 },
      { key: "umbrella_stand", x: 460, y: 590, scale: 1.5 },
      { key: "coat_rack", x: 430, y: 590, scale: 1.5 },
      { key: "filing_cabinet", x: 28, y: 350, scale: 1.5 },
      { key: "filing_cabinet", x: 28, y: 420, scale: 1.5 },
      { key: "fire_extinguisher", x: 22, y: 560, scale: 1.5 },
      { key: "desk_lamp", x: 175, y: 265, scale: 1.5, depth: 270 },
      { key: "desk_lamp", x: 495, y: 395, scale: 1.5, depth: 400 },
    ],
    wallItems: [
      { key: "whiteboard", x: 260, y: 55, scale: 1.6 },
      { key: "bulletin_board", x: 370, y: 62, scale: 1.3 },
      { key: "picture_frame", x: 600, y: 55, scale: 1.4 },
      { key: "picture_frame", x: 660, y: 52, scale: 1.2, tint: 0xddccaa },
      { key: "poster", x: 730, y: 55, scale: 1.5, tint: 0xccaa88 },
      { key: "clock", x: 820, y: 55, scale: 2.2 },
      { key: "ac_unit", x: 140, y: 110, scale: 1.5 },
      { key: "wall_shelf", x: 530, y: 75, scale: 1.3 },
    ],
    sheetItems: [
      { x: 880, y: 110, sheet: "furniture", sx: 192, sy: 96, sw: 64, sh: 64, scale: 2.0, depth: 3 },
      { x: 880, y: 180, sheet: "furniture", sx: 192, sy: 160, sw: 64, sh: 64, scale: 2.0, depth: 3 },
    ],
  },
  { // 1 — Executive Suite: spacious 2×2 desks, conference area, rich decor
    deskSlots: [
      { x: 180, y: 280 }, { x: 420, y: 280 },
      { x: 180, y: 420 }, { x: 420, y: 420 },
    ],
    idleSlots: [
      { x: 720, y: 260 }, { x: 760, y: 300 }, { x: 700, y: 340 },
      { x: 740, y: 380 },
    ],
    wanderPoints: [
      { x: 720, y: 260 }, { x: 760, y: 300 }, { x: 700, y: 340 },
      { x: 650, y: 450 }, { x: 800, y: 280 }, { x: 550, y: 500 },
      { x: 870, y: 350 }, { x: 400, y: 550 }, { x: 300, y: 350 },
    ],
    catWanderPoints: [
      { x: 80, y: 560 }, { x: 200, y: 500 }, { x: 400, y: 530 },
      { x: 600, y: 550 }, { x: 300, y: 450 }, { x: 500, y: 480 },
      { x: 720, y: 350 }, { x: 100, y: 300 },
    ],
    actionSpots: {
      coffee: { x: 870, y: 210 }, waterCooler: { x: 910, y: 210 }, sofa: { x: 720, y: 230 },
      whiteboard: { x: 200, y: 160 }, server: { x: 935, y: 320 }, door: { x: GW / 2, y: GH - 60 },
      plant1: { x: 40, y: 480 }, plant2: { x: 920, y: 450 }, window: { x: 330, y: 160 },
      trophy: { x: 600, y: 200 }, bookshelf: { x: 28, y: 240 }, fishTank: { x: 700, y: 500 },
    },
    windows: [{ x: 300, y: 30 }],
    ceilingLights: [200, 400, 700],
    floorItems: [
      { key: "rug", x: 720, y: 280, scale: 2.5, depth: 1 },
      { key: "rug", x: 300, y: 350, scale: 1.8, depth: 1 },
      { key: "sofa", x: 720, y: 200, scale: 1.6 },
      { key: "round_table", x: 750, y: 270, scale: 1.6 },
      { key: "paper_stack", x: 740, y: 265, scale: 1.3 },
      { key: "coffee_cup", x: 760, y: 260, scale: 1.5 },
      { key: "snack_bowl", x: 770, y: 268, scale: 1.3 },
      { key: "coffee", x: 870, y: 190, scale: 1.5 },
      { key: "water_cooler", x: 910, y: 190, scale: 1.5 },
      { key: "server", x: 935, y: 300, scale: 1.3 },
      { key: "trophy_case", x: 600, y: 185, scale: 2.0, depth: 3 },
      { key: "globe", x: 560, y: 200, scale: 2.0 },
      { key: "bookshelf", x: 28, y: 240, scale: 2.0, depth: 3 },
      { key: "fish_tank", x: 700, y: 490, scale: 2.0, depth: 510 },
      { key: "plant", x: 35, y: 480, scale: 2.0 },
      { key: "plant", x: 935, y: 450, scale: 1.6 },
      { key: "plant", x: 935, y: 160, scale: 1.3 },
      { key: "potted_flower", x: 500, y: 530, scale: 1.6 },
      { key: "small_plant", x: 28, y: 200, scale: 1.8 },
      { key: "small_plant", x: 600, y: 530, scale: 1.5 },
      { key: "umbrella_stand", x: 460, y: 590, scale: 1.5 },
      { key: "coat_rack", x: 440, y: 585, scale: 1.5 },
      { key: "filing_cabinet", x: 28, y: 330, scale: 1.5 },
      { key: "filing_cabinet", x: 28, y: 400, scale: 1.5 },
      { key: "filing_cabinet", x: 28, y: 470, scale: 1.5 },
      { key: "fire_extinguisher", x: 22, y: 530, scale: 1.5 },
      { key: "desk_lamp", x: 195, y: 265, scale: 1.5, depth: 270 },
      { key: "desk_lamp", x: 435, y: 265, scale: 1.5, depth: 270 },
    ],
    wallItems: [
      { key: "whiteboard", x: 200, y: 55, scale: 1.6 },
      { key: "picture_frame", x: 450, y: 52, scale: 1.5 },
      { key: "picture_frame", x: 520, y: 55, scale: 1.3, tint: 0xddccaa },
      { key: "picture_frame", x: 650, y: 55, scale: 1.4 },
      { key: "picture_frame", x: 720, y: 52, scale: 1.2, tint: 0xccbbaa },
      { key: "clock", x: 820, y: 55, scale: 2.2 },
      { key: "poster", x: 560, y: 55, scale: 1.4, tint: 0xccaa88 },
      { key: "ac_unit", x: 140, y: 110, scale: 1.5 },
      { key: "wall_shelf", x: 400, y: 75, scale: 1.2 },
    ],
    sheetItems: [
      { x: 880, y: 110, sheet: "furniture", sx: 192, sy: 96, sw: 64, sh: 64, scale: 2.0, depth: 3 },
      { x: 880, y: 180, sheet: "furniture", sx: 192, sy: 160, sw: 64, sh: 64, scale: 2.0, depth: 3 },
      { x: 880, y: 380, sheet: "furniture", sx: 192, sy: 96, sw: 64, sh: 64, scale: 2.0, depth: 3 },
    ],
  },
  { // 2 — Creative Studio: L-shape desks, 3 windows, lots of plants, whiteboard wall
    deskSlots: [
      { x: 140, y: 280 }, { x: 300, y: 280 }, { x: 460, y: 280 },
      { x: 140, y: 420 }, { x: 300, y: 420 },
    ],
    idleSlots: [
      { x: 700, y: 260 }, { x: 720, y: 330 }, { x: 680, y: 400 },
      { x: 750, y: 370 }, { x: 650, y: 480 },
    ],
    wanderPoints: [
      { x: 700, y: 260 }, { x: 720, y: 330 }, { x: 680, y: 400 },
      { x: 750, y: 370 }, { x: 650, y: 480 }, { x: 800, y: 280 },
      { x: 550, y: 500 }, { x: 400, y: 480 }, { x: 300, y: 530 },
      { x: 870, y: 350 }, { x: 200, y: 500 },
    ],
    catWanderPoints: [
      { x: 80, y: 560 }, { x: 200, y: 500 }, { x: 400, y: 530 },
      { x: 600, y: 550 }, { x: 300, y: 450 }, { x: 500, y: 480 },
      { x: 720, y: 350 }, { x: 100, y: 400 }, { x: 870, y: 420 },
    ],
    actionSpots: {
      coffee: { x: 850, y: 300 }, waterCooler: { x: 890, y: 300 }, sofa: { x: 700, y: 240 },
      whiteboard: { x: 200, y: 160 }, door: { x: GW / 2, y: GH - 60 },
      plant1: { x: 40, y: 480 }, plant2: { x: 800, y: 450 }, window: { x: 110, y: 160 },
      guitar: { x: 930, y: 350 }, snack: { x: 550, y: 500 },
    },
    windows: [{ x: 80, y: 30 }, { x: 260, y: 30 }, { x: 440, y: 30 }],
    ceilingLights: [150, 300, 450, 600, 780],
    floorItems: [
      { key: "rug", x: 400, y: 400, scale: 2.5, depth: 1 },
      { key: "rug", x: 720, y: 270, scale: 1.5, depth: 1 },
      { key: "sofa", x: 700, y: 210, scale: 1.6 },
      { key: "round_table", x: 550, y: 500, scale: 1.5 },
      { key: "coffee_cup", x: 540, y: 495, scale: 1.4 },
      { key: "paper_stack", x: 560, y: 495, scale: 1.2 },
      { key: "snack_bowl", x: 550, y: 490, scale: 1.3 },
      { key: "pizza_box", x: 530, y: 498, scale: 1.4 },
      { key: "coffee", x: 850, y: 280, scale: 1.5 },
      { key: "water_cooler", x: 890, y: 280, scale: 1.5 },
      { key: "easel", x: 650, y: 440, scale: 2.0, depth: 460 },
      { key: "guitar", x: 930, y: 330, scale: 2.0, depth: 370 },
      { key: "speaker", x: 920, y: 470, scale: 1.8 },
      { key: "speaker", x: 920, y: 510, scale: 1.8 },
      { key: "bamboo_divider", x: 600, y: 350, scale: 2.0, depth: 370 },
      { key: "plant", x: 35, y: 480, scale: 2.0 },
      { key: "plant", x: 935, y: 420, scale: 1.6 },
      { key: "plant", x: 550, y: 530, scale: 1.4 },
      { key: "plant", x: 280, y: 530, scale: 1.3 },
      { key: "plant", x: 800, y: 450, scale: 1.5 },
      { key: "potted_flower", x: 350, y: 530, scale: 1.5 },
      { key: "potted_flower", x: 700, y: 530, scale: 1.4 },
      { key: "small_plant", x: 28, y: 200, scale: 1.8 },
      { key: "small_plant", x: 935, y: 160, scale: 1.5 },
      { key: "small_plant", x: 450, y: 530, scale: 1.3 },
      { key: "coat_rack", x: 430, y: 585, scale: 1.5 },
      { key: "filing_cabinet", x: 28, y: 380, scale: 1.5 },
      { key: "fire_extinguisher", x: 22, y: 530, scale: 1.5 },
    ],
    wallItems: [
      { key: "whiteboard", x: 200, y: 55, scale: 1.8 },
      { key: "whiteboard", x: 350, y: 55, scale: 1.4 },
      { key: "bulletin_board", x: 480, y: 62, scale: 1.3 },
      { key: "neon_sign", x: 630, y: 62, scale: 1.8 },
      { key: "poster", x: 730, y: 55, scale: 1.5, tint: 0xccaa88 },
      { key: "poster", x: 800, y: 55, scale: 1.3, tint: 0xaabbcc },
      { key: "clock", x: 880, y: 55, scale: 2.2 },
      { key: "ac_unit", x: 140, y: 110, scale: 1.5 },
    ],
    sheetItems: [
      { x: 880, y: 180, sheet: "furniture", sx: 192, sy: 160, sw: 64, sh: 64, scale: 2.0, depth: 3 },
    ],
  },
  { // 3 — Tech Lab: 3×2 desks, multi-server, vending machines, utilitarian
    deskSlots: [
      { x: 160, y: 240 }, { x: 380, y: 240 },
      { x: 160, y: 350 }, { x: 380, y: 350 },
      { x: 160, y: 460 }, { x: 380, y: 460 },
    ],
    idleSlots: [
      { x: 700, y: 260 }, { x: 720, y: 330 }, { x: 750, y: 400 },
      { x: 680, y: 470 }, { x: 700, y: 220 }, { x: 740, y: 360 },
    ],
    wanderPoints: [
      { x: 700, y: 260 }, { x: 720, y: 330 }, { x: 750, y: 400 },
      { x: 680, y: 470 }, { x: 800, y: 280 }, { x: 870, y: 350 },
      { x: 550, y: 500 }, { x: 600, y: 320 }, { x: 500, y: 400 },
    ],
    catWanderPoints: [
      { x: 80, y: 560 }, { x: 200, y: 500 }, { x: 400, y: 530 },
      { x: 600, y: 550 }, { x: 300, y: 450 }, { x: 500, y: 480 },
      { x: 870, y: 330 }, { x: 100, y: 400 },
    ],
    actionSpots: {
      coffee: { x: 930, y: 210 }, waterCooler: { x: 930, y: 280 }, server: { x: 870, y: 240 },
      whiteboard: { x: 260, y: 160 }, door: { x: GW / 2, y: GH - 60 },
      plant1: { x: 40, y: 500 }, plant2: { x: 600, y: 530 }, window: { x: 250, y: 160 },
      snack: { x: 930, y: 450 }, fridge: { x: 930, y: 480 },
    },
    windows: [{ x: 220, y: 30 }],
    ceilingLights: [180, 380, 700, 880],
    floorItems: [
      { key: "server", x: 870, y: 200, scale: 1.3 },
      { key: "server", x: 910, y: 200, scale: 1.3 },
      { key: "server", x: 870, y: 270, scale: 1.3 },
      { key: "server", x: 910, y: 270, scale: 1.3 },
      { key: "vending_machine", x: 930, y: 340, scale: 1.4 },
      { key: "vending_machine", x: 930, y: 410, scale: 1.4 },
      { key: "mini_fridge", x: 930, y: 470, scale: 1.5 },
      { key: "coffee", x: 930, y: 190, scale: 1.5 },
      { key: "water_cooler", x: 930, y: 260, scale: 1.5 },
      { key: "standing_desk", x: 600, y: 300, scale: 2.0, depth: 330 },
      { key: "standing_desk", x: 660, y: 300, scale: 2.0, depth: 330 },
      { key: "snack_bowl", x: 700, y: 400, scale: 1.3 },
      { key: "pizza_box", x: 720, y: 400, scale: 1.4 },
      { key: "plant", x: 35, y: 500, scale: 1.8 },
      { key: "plant", x: 600, y: 530, scale: 1.4 },
      { key: "cactus", x: 550, y: 530, scale: 1.8 },
      { key: "small_plant", x: 28, y: 200, scale: 1.8 },
      { key: "desk_lamp", x: 175, y: 225, scale: 1.5, depth: 230 },
      { key: "desk_lamp", x: 395, y: 335, scale: 1.5, depth: 340 },
      { key: "filing_cabinet", x: 28, y: 300, scale: 1.5 },
      { key: "filing_cabinet", x: 28, y: 370, scale: 1.5 },
      { key: "filing_cabinet", x: 28, y: 440, scale: 1.5 },
      { key: "filing_cabinet", x: 28, y: 510, scale: 1.5 },
      { key: "coat_rack", x: 430, y: 585, scale: 1.5 },
      { key: "fire_extinguisher", x: 22, y: 540, scale: 1.5 },
      { key: "fire_extinguisher", x: 935, y: 530, scale: 1.5 },
    ],
    wallItems: [
      { key: "whiteboard", x: 260, y: 55, scale: 1.6 },
      { key: "clock_modern", x: 360, y: 58, scale: 2.0 },
      { key: "ac_unit", x: 140, y: 110, scale: 1.5 },
      { key: "ac_unit", x: 800, y: 110, scale: 1.5 },
      { key: "neon_sign", x: 500, y: 62, scale: 1.5 },
      { key: "poster", x: 620, y: 55, scale: 1.4, tint: 0xaabbcc },
      { key: "bulletin_board", x: 720, y: 62, scale: 1.3 },
      { key: "projector_screen", x: 600, y: 155, scale: 1.4, depth: 2 },
    ],
    sheetItems: [],
  },
  { // 4 — Garden Office: outdoor terrace with trees, fountain, patio seating
    deskSlots: [
      { x: 140, y: 280 }, { x: 300, y: 280 },
      { x: 140, y: 400 }, { x: 300, y: 400 },
    ],
    idleSlots: [
      { x: 650, y: 320 }, { x: 700, y: 380 }, { x: 620, y: 450 },
      { x: 750, y: 280 },
    ],
    wanderPoints: [
      { x: 650, y: 320 }, { x: 700, y: 380 }, { x: 620, y: 450 },
      { x: 750, y: 280 }, { x: 550, y: 500 }, { x: 800, y: 400 },
      { x: 400, y: 520 }, { x: 870, y: 300 }, { x: 200, y: 500 },
    ],
    catWanderPoints: [
      { x: 80, y: 540 }, { x: 200, y: 480 }, { x: 400, y: 510 },
      { x: 600, y: 530 }, { x: 750, y: 400 }, { x: 870, y: 350 },
      { x: 500, y: 350 }, { x: 300, y: 450 },
    ],
    actionSpots: {
      coffee: { x: 560, y: 200 }, sofa: { x: 680, y: 260 },
      whiteboard: { x: 200, y: 160 }, door: { x: GW / 2, y: GH - 60 },
      plant1: { x: 820, y: 200 }, plant2: { x: 100, y: 480 },
      window: { x: 130, y: 160 }, fountain: { x: 750, y: 450 },
      bench: { x: 660, y: 340 }, hammock: { x: 790, y: 320 },
      telescope: { x: 900, y: 250 },
    },
    windows: [{ x: 100, y: 30 }, { x: 300, y: 30 }],
    ceilingLights: [160, 350],
    floorItems: [
      // Garden area (right side)
      { key: "grass_patch", x: 600, y: 520, scale: 3.0, depth: 1 },
      { key: "grass_patch", x: 750, y: 500, scale: 2.5, depth: 1 },
      { key: "grass_patch", x: 870, y: 480, scale: 2.8, depth: 1 },
      { key: "grass_patch", x: 680, y: 560, scale: 2.5, depth: 1 },
      { key: "garden_tree", x: 820, y: 200, scale: 2.8, depth: 250 },
      { key: "garden_tree", x: 920, y: 280, scale: 2.5, depth: 330 },
      { key: "garden_tree", x: 900, y: 480, scale: 2.2, depth: 530 },
      { key: "garden_bush", x: 700, y: 500, scale: 2.5, depth: 1 },
      { key: "garden_bush", x: 860, y: 400, scale: 2.0, depth: 1 },
      { key: "garden_bush", x: 580, y: 560, scale: 2.2, depth: 1 },
      { key: "flower_bed", x: 650, y: 540, scale: 2.0, depth: 1 },
      { key: "flower_bed", x: 800, y: 520, scale: 1.8, depth: 1 },
      { key: "fountain", x: 750, y: 430, scale: 2.5, depth: 460 },
      { key: "stone_path", x: 550, y: 490, scale: 2.5, depth: 1 },
      { key: "stone_path", x: 620, y: 490, scale: 2.5, depth: 1 },
      { key: "stone_path", x: 690, y: 490, scale: 2.5, depth: 1 },
      { key: "garden_fence", x: 540, y: 200, scale: 2.0, depth: 2 },
      { key: "garden_fence", x: 540, y: 540, scale: 2.0, depth: 2 },
      { key: "patio_umbrella", x: 650, y: 280, scale: 2.0, depth: 310 },
      { key: "bench", x: 660, y: 340, scale: 2.0, depth: 350 },
      { key: "bird_bath", x: 880, y: 360, scale: 2.0, depth: 380 },
      { key: "garden_lamp", x: 560, y: 350, scale: 2.0, depth: 370 },
      { key: "garden_lamp", x: 560, y: 500, scale: 2.0, depth: 520 },
      { key: "hammock", x: 790, y: 320, scale: 1.8, depth: 340 },
      { key: "telescope", x: 900, y: 230, scale: 2.0, depth: 260 },
      { key: "planter_box", x: 700, y: 560, scale: 2.0, depth: 1 },
      { key: "planter_box", x: 830, y: 550, scale: 1.8, depth: 1 },
      // Indoor work area
      { key: "rug", x: 220, y: 340, scale: 2.5, depth: 1 },
      { key: "coffee", x: 560, y: 180, scale: 1.5 },
      { key: "water_cooler", x: 500, y: 180, scale: 1.5 },
      { key: "plant", x: 100, y: 480, scale: 2.0 },
      { key: "cactus", x: 35, y: 300, scale: 2.0 },
      { key: "potted_flower", x: 35, y: 400, scale: 1.8 },
      { key: "small_plant", x: 28, y: 200, scale: 1.8 },
      { key: "desk_lamp", x: 155, y: 265, scale: 1.5, depth: 270 },
      { key: "coat_rack", x: 430, y: 585, scale: 1.5 },
    ],
    wallItems: [
      { key: "whiteboard", x: 200, y: 55, scale: 1.6 },
      { key: "picture_frame", x: 350, y: 55, scale: 1.4 },
      { key: "clock", x: 440, y: 55, scale: 2.2 },
      { key: "wall_shelf", x: 300, y: 75, scale: 1.2 },
    ],
    sheetItems: [],
  },
  { // 5 — Cozy Lounge: beanbags, bookshelves, loveseat, warm feel
    deskSlots: [
      { x: 160, y: 280 }, { x: 320, y: 280 },
      { x: 160, y: 420 },
    ],
    idleSlots: [
      { x: 650, y: 260 }, { x: 700, y: 320 }, { x: 750, y: 380 },
    ],
    wanderPoints: [
      { x: 650, y: 260 }, { x: 700, y: 320 }, { x: 750, y: 380 },
      { x: 550, y: 450 }, { x: 800, y: 280 }, { x: 400, y: 500 },
      { x: 870, y: 340 }, { x: 300, y: 530 },
    ],
    catWanderPoints: [
      { x: 80, y: 540 }, { x: 300, y: 500 }, { x: 500, y: 520 },
      { x: 700, y: 350 }, { x: 850, y: 300 }, { x: 200, y: 400 },
    ],
    actionSpots: {
      coffee: { x: 870, y: 300 }, waterCooler: { x: 910, y: 300 },
      sofa: { x: 650, y: 230 }, whiteboard: { x: 200, y: 160 },
      door: { x: GW / 2, y: GH - 60 },
      plant1: { x: 40, y: 500 }, plant2: { x: 935, y: 440 },
      window: { x: 130, y: 160 },
      bookshelf: { x: 28, y: 300 }, snack: { x: 700, y: 280 },
      guitar: { x: 850, y: 450 }, fishTank: { x: 850, y: 200 },
    },
    windows: [{ x: 100, y: 30 }, { x: 350, y: 30 }],
    ceilingLights: [180, 400, 700],
    floorItems: [
      { key: "rug_large", x: 700, y: 300, scale: 3.0, depth: 1 },
      { key: "rug_modern", x: 250, y: 350, scale: 2.5, depth: 1 },
      { key: "loveseat", x: 650, y: 210, scale: 2.5, depth: 240 },
      { key: "beanbag", x: 750, y: 280, scale: 2.5, depth: 300 },
      { key: "beanbag", x: 800, y: 340, scale: 2.2, depth: 360, tint: 0xcc88ff },
      { key: "beanbag_green", x: 700, y: 390, scale: 2.2, depth: 410 },
      { key: "recliner", x: 620, y: 320, scale: 2.2, depth: 340 },
      { key: "round_table", x: 700, y: 280, scale: 1.6 },
      { key: "coffee_cup", x: 690, y: 275, scale: 1.5 },
      { key: "snack_bowl", x: 710, y: 275, scale: 1.3 },
      { key: "fish_tank", x: 850, y: 185, scale: 2.0 },
      { key: "guitar", x: 850, y: 430, scale: 2.0, depth: 470 },
      { key: "speaker", x: 820, y: 460, scale: 1.5 },
      { key: "bookshelf", x: 28, y: 240, scale: 2.0, depth: 3 },
      { key: "bookshelf", x: 28, y: 350, scale: 2.0, depth: 3 },
      { key: "bookshelf", x: 28, y: 460, scale: 2.0, depth: 3 },
      { key: "coffee", x: 870, y: 280, scale: 1.5 },
      { key: "water_cooler", x: 910, y: 280, scale: 1.5 },
      { key: "plant", x: 35, y: 500, scale: 2.0 },
      { key: "plant", x: 935, y: 440, scale: 1.6 },
      { key: "cactus", x: 935, y: 200, scale: 2.0 },
      { key: "small_plant", x: 550, y: 530, scale: 1.5 },
      { key: "small_plant", x: 450, y: 210, scale: 1.6 },
      { key: "coat_rack", x: 430, y: 585, scale: 1.5 },
      { key: "yoga_mat", x: 500, y: 470, scale: 2.0, depth: 1 },
      { key: "filing_cabinet", x: 935, y: 350, scale: 1.5 },
    ],
    wallItems: [
      { key: "whiteboard", x: 200, y: 55, scale: 1.6 },
      { key: "picture_frame", x: 500, y: 52, scale: 1.5 },
      { key: "picture_frame", x: 570, y: 55, scale: 1.3, tint: 0xddccaa },
      { key: "picture_frame", x: 640, y: 52, scale: 1.4 },
      { key: "poster", x: 730, y: 55, scale: 1.5, tint: 0xccaa88 },
      { key: "poster", x: 800, y: 55, scale: 1.3, tint: 0xaabbcc },
      { key: "clock", x: 880, y: 55, scale: 2.2 },
      { key: "ac_unit", x: 140, y: 110, scale: 1.5 },
    ],
    sheetItems: [],
  },
  { // 6 — Game Room: ping pong, arcade, pool table, fun zone
    deskSlots: [
      { x: 140, y: 260 }, { x: 300, y: 260 },
      { x: 140, y: 380 }, { x: 300, y: 380 },
    ],
    idleSlots: [
      { x: 600, y: 280 }, { x: 700, y: 350 }, { x: 800, y: 280 },
      { x: 650, y: 420 },
    ],
    wanderPoints: [
      { x: 600, y: 280 }, { x: 700, y: 350 }, { x: 800, y: 280 },
      { x: 650, y: 420 }, { x: 550, y: 480 }, { x: 870, y: 350 },
      { x: 400, y: 520 }, { x: 750, y: 450 },
    ],
    catWanderPoints: [
      { x: 80, y: 540 }, { x: 300, y: 500 }, { x: 500, y: 530 },
      { x: 700, y: 450 }, { x: 850, y: 350 }, { x: 150, y: 400 },
    ],
    actionSpots: {
      coffee: { x: 500, y: 200 }, waterCooler: { x: 540, y: 200 },
      sofa: { x: 600, y: 230 }, whiteboard: { x: 200, y: 160 },
      door: { x: GW / 2, y: GH - 60 },
      plant1: { x: 40, y: 500 }, plant2: { x: 935, y: 540 },
      window: { x: 130, y: 160 },
      arcade: { x: 920, y: 260 }, pingpong: { x: 620, y: 260 },
      foosball: { x: 700, y: 480 }, dartboard: { x: 850, y: 200 },
      snack: { x: 560, y: 450 }, fridge: { x: 930, y: 470 },
    },
    windows: [{ x: 100, y: 30 }, { x: 400, y: 30 }],
    ceilingLights: [180, 350, 600, 800],
    floorItems: [
      { key: "rug_modern", x: 700, y: 320, scale: 3.5, depth: 1 },
      { key: "rug", x: 220, y: 320, scale: 2.0, depth: 1 },
      { key: "ping_pong", x: 620, y: 260, scale: 2.5, depth: 290 },
      { key: "pool_table", x: 800, y: 350, scale: 2.5, depth: 380 },
      { key: "foosball", x: 700, y: 460, scale: 2.2, depth: 490 },
      { key: "arcade_machine", x: 920, y: 220, scale: 2.0, depth: 260 },
      { key: "arcade_machine", x: 920, y: 300, scale: 2.0, depth: 340 },
      { key: "sofa", x: 600, y: 200, scale: 1.6 },
      { key: "beanbag", x: 700, y: 440, scale: 2.5, depth: 460 },
      { key: "beanbag", x: 780, y: 460, scale: 2.2, depth: 480, tint: 0x88ccff },
      { key: "round_table", x: 560, y: 450, scale: 1.5, depth: 470 },
      { key: "coffee_cup", x: 550, y: 445, scale: 1.4 },
      { key: "coffee", x: 500, y: 180, scale: 1.5 },
      { key: "water_cooler", x: 540, y: 180, scale: 1.5 },
      { key: "vending_machine", x: 930, y: 400, scale: 1.4 },
      { key: "mini_fridge", x: 930, y: 470, scale: 1.5 },
      { key: "trophy_case", x: 600, y: 200, scale: 2.0 },
      { key: "snack_bowl", x: 550, y: 445, scale: 1.3 },
      { key: "pizza_box", x: 570, y: 448, scale: 1.4 },
      { key: "plant", x: 35, y: 500, scale: 2.0 },
      { key: "plant", x: 935, y: 540, scale: 1.3 },
      { key: "small_plant", x: 28, y: 200, scale: 1.8 },
      { key: "coat_rack", x: 430, y: 585, scale: 1.5 },
      { key: "filing_cabinet", x: 28, y: 330, scale: 1.5 },
      { key: "fire_extinguisher", x: 22, y: 540, scale: 1.5 },
    ],
    wallItems: [
      { key: "whiteboard", x: 200, y: 55, scale: 1.6 },
      { key: "neon_sign", x: 350, y: 62, scale: 2.0 },
      { key: "dartboard", x: 530, y: 55, scale: 2.0 },
      { key: "poster", x: 650, y: 55, scale: 1.3, tint: 0xff44aa },
      { key: "poster", x: 730, y: 55, scale: 1.4, tint: 0x44aaff },
      { key: "clock", x: 860, y: 55, scale: 2.2 },
    ],
    sheetItems: [],
  },
  { // 7 — Wellness Studio: treadmill, yoga, garden corner, standing desks
    deskSlots: [
      { x: 160, y: 270 }, { x: 340, y: 270 },
      { x: 160, y: 400 },
    ],
    idleSlots: [
      { x: 650, y: 280 }, { x: 700, y: 380 }, { x: 600, y: 450 },
    ],
    wanderPoints: [
      { x: 650, y: 280 }, { x: 700, y: 380 }, { x: 600, y: 450 },
      { x: 800, y: 300 }, { x: 550, y: 520 }, { x: 870, y: 400 },
      { x: 400, y: 500 }, { x: 300, y: 450 },
    ],
    catWanderPoints: [
      { x: 80, y: 540 }, { x: 250, y: 500 }, { x: 450, y: 530 },
      { x: 650, y: 450 }, { x: 820, y: 350 }, { x: 150, y: 380 },
    ],
    actionSpots: {
      coffee: { x: 500, y: 200 }, waterCooler: { x: 540, y: 200 },
      sofa: { x: 650, y: 240 }, whiteboard: { x: 200, y: 160 },
      door: { x: GW / 2, y: GH - 60 },
      plant1: { x: 820, y: 450 }, plant2: { x: 40, y: 500 },
      window: { x: 130, y: 160 },
      meditation: { x: 650, y: 470 }, yoga: { x: 650, y: 400 },
      bench: { x: 830, y: 540 },
    },
    windows: [{ x: 100, y: 30 }, { x: 300, y: 30 }, { x: 500, y: 30 }],
    ceilingLights: [160, 340, 600, 800],
    floorItems: [
      { key: "rug_large", x: 250, y: 340, scale: 2.5, depth: 1 },
      // Wellness zone
      { key: "treadmill", x: 870, y: 220, scale: 2.0, depth: 260 },
      { key: "treadmill", x: 920, y: 300, scale: 2.0, depth: 340 },
      { key: "yoga_mat", x: 650, y: 400, scale: 2.5, depth: 1 },
      { key: "yoga_mat", x: 720, y: 430, scale: 2.5, depth: 1, tint: 0x22aacc },
      { key: "yoga_mat", x: 580, y: 430, scale: 2.5, depth: 1, tint: 0xee6688 },
      { key: "meditation_cushion", x: 650, y: 460, scale: 2.0, depth: 1 },
      { key: "meditation_cushion", x: 710, y: 470, scale: 2.0, depth: 1, tint: 0x44bbaa },
      // Standing desk area
      { key: "standing_desk", x: 500, y: 280, scale: 2.0, depth: 310 },
      { key: "standing_desk", x: 560, y: 280, scale: 2.0, depth: 310 },
      // Garden corner
      { key: "grass_patch", x: 780, y: 490, scale: 2.5, depth: 1 },
      { key: "grass_patch", x: 870, y: 510, scale: 2.0, depth: 1 },
      { key: "garden_tree", x: 820, y: 430, scale: 2.5, depth: 480 },
      { key: "garden_bush", x: 880, y: 470, scale: 2.0, depth: 490 },
      { key: "flower_bed", x: 750, y: 530, scale: 2.0, depth: 1 },
      { key: "cactus", x: 935, y: 400, scale: 2.0 },
      // Lounge
      { key: "loveseat", x: 650, y: 210, scale: 2.2, depth: 240 },
      { key: "beanbag", x: 750, y: 260, scale: 2.0, depth: 280 },
      { key: "round_table", x: 700, y: 250, scale: 1.4 },
      { key: "coffee_cup", x: 690, y: 245, scale: 1.4 },
      // Utilities
      { key: "coffee", x: 500, y: 180, scale: 1.5 },
      { key: "water_cooler", x: 540, y: 180, scale: 1.5 },
      { key: "bamboo_divider", x: 480, y: 330, scale: 2.5, depth: 350 },
      { key: "plant", x: 35, y: 500, scale: 2.0 },
      { key: "plant", x: 600, y: 540, scale: 1.3 },
      { key: "potted_flower", x: 450, y: 530, scale: 1.5 },
      { key: "small_plant", x: 28, y: 200, scale: 1.8 },
      { key: "small_plant", x: 450, y: 200, scale: 1.5 },
      { key: "bench", x: 830, y: 540, scale: 2.0, depth: 555 },
      { key: "coat_rack", x: 430, y: 585, scale: 1.5 },
      { key: "speaker", x: 935, y: 180, scale: 1.5 },
    ],
    wallItems: [
      { key: "whiteboard", x: 200, y: 55, scale: 1.6 },
      { key: "picture_frame", x: 400, y: 55, scale: 1.4 },
      { key: "poster", x: 650, y: 55, scale: 1.5, tint: 0x66cc99 },
      { key: "poster", x: 750, y: 55, scale: 1.3, tint: 0xcc9966 },
      { key: "clock", x: 850, y: 55, scale: 2.2 },
      { key: "ac_unit", x: 140, y: 110, scale: 1.5 },
      { key: "wall_shelf", x: 550, y: 75, scale: 1.2 },
    ],
    sheetItems: [],
  },
];

// Agent/cat actions — use spot key strings resolved at runtime from active layout
type AgentActionDef = { name: string; spotKey: string; bubble: string; duration: number };
const IDLE_ACTION_DEFS: AgentActionDef[] = [
  { name: "coffee",     spotKey: "coffee",      bubble: t("page.manor_office.idle_action_bubble_01"),    duration: 5000 },
  { name: "coffee",     spotKey: "coffee",      bubble: t("page.manor_office.idle_action_bubble_02"),       duration: 4000 },
  { name: "coffee",     spotKey: "coffee",      bubble: t("page.manor_office.idle_action_bubble_03"),       duration: 4500 },
  { name: "water",      spotKey: "waterCooler", bubble: t("page.manor_office.idle_action_bubble_04"),     duration: 4000 },
  { name: "water",      spotKey: "waterCooler", bubble: t("page.manor_office.idle_action_bubble_05"),    duration: 3500 },
  { name: "sofa",       spotKey: "sofa",        bubble: t("page.manor_office.idle_action_bubble_06"),         duration: 8000 },
  { name: "sofa",       spotKey: "sofa",        bubble: t("page.manor_office.idle_action_bubble_07"),      duration: 6000 },
  { name: "sofa",       spotKey: "sofa",        bubble: t("page.manor_office.idle_action_bubble_08"),       duration: 7000 },
  { name: "whiteboard", spotKey: "whiteboard",  bubble: t("page.manor_office.idle_action_bubble_09"),   duration: 5000 },
  { name: "whiteboard", spotKey: "whiteboard",  bubble: t("page.manor_office.idle_action_bubble_10"),       duration: 4000 },
  { name: "whiteboard", spotKey: "whiteboard",  bubble: t("page.manor_office.idle_action_bubble_11"),     duration: 5500 },
  { name: "door",       spotKey: "door",        bubble: t("page.manor_office.idle_action_bubble_12"),     duration: 6000 },
  { name: "door",       spotKey: "door",        bubble: t("page.manor_office.idle_action_bubble_13"),     duration: 5000 },
  { name: "plant",      spotKey: "plant1",      bubble: t("page.manor_office.idle_action_bubble_14"),          duration: 3000 },
  { name: "plant",      spotKey: "plant1",      bubble: t("page.manor_office.idle_action_bubble_15"),  duration: 4000 },
  { name: "fountain",   spotKey: "fountain",    bubble: t("page.manor_office.idle_action_bubble_16"),         duration: 5000 },
  { name: "fountain",   spotKey: "fountain",    bubble: t("page.manor_office.idle_action_bubble_17"),   duration: 4000 },
  { name: "snack",      spotKey: "snack",       bubble: t("page.manor_office.idle_action_bubble_18"),          duration: 3500 },
  { name: "snack",      spotKey: "snack",       bubble: t("page.manor_office.idle_action_bubble_19"),     duration: 4000 },
  { name: "fridge",     spotKey: "fridge",       bubble: t("page.manor_office.idle_action_bubble_20"),   duration: 3000 },
  { name: "fish",       spotKey: "fishTank",    bubble: t("page.manor_office.idle_action_bubble_21"),     duration: 4000 },
  { name: "fish",       spotKey: "fishTank",    bubble: t("page.manor_office.idle_action_bubble_22"),        duration: 5000 },
  { name: "trophy",     spotKey: "trophy",      bubble: t("page.manor_office.idle_action_bubble_23"),      duration: 3500 },
  { name: "bookshelf",  spotKey: "bookshelf",   bubble: t("page.manor_office.idle_action_bubble_24"),    duration: 4500 },
  { name: "bookshelf",  spotKey: "bookshelf",   bubble: t("page.manor_office.idle_action_bubble_25"),      duration: 4000 },
  { name: "guitar",     spotKey: "guitar",      bubble: t("page.manor_office.idle_action_bubble_26"),    duration: 6000 },
  { name: "arcade",     spotKey: "arcade",      bubble: t("page.manor_office.idle_action_bubble_27"),    duration: 5000 },
  { name: "arcade",     spotKey: "arcade",      bubble: t("page.manor_office.idle_action_bubble_28"),         duration: 4500 },
  { name: "pingpong",   spotKey: "pingpong",    bubble: t("page.manor_office.idle_action_bubble_29"), duration: 5000 },
  { name: "foosball",   spotKey: "foosball",    bubble: t("page.manor_office.idle_action_bubble_30"),              duration: 4000 },
  { name: "dartboard",  spotKey: "dartboard",   bubble: t("page.manor_office.idle_action_bubble_31"),            duration: 3500 },
  { name: "telescope",  spotKey: "telescope",   bubble: t("page.manor_office.idle_action_bubble_32"),        duration: 5000 },
  { name: "bench",      spotKey: "bench",       bubble: t("page.manor_office.idle_action_bubble_33"),     duration: 6000 },
  { name: "hammock",    spotKey: "hammock",     bubble: t("page.manor_office.idle_action_bubble_34"),    duration: 7000 },
  { name: "meditate",   spotKey: "meditation",  bubble: t("page.manor_office.idle_action_bubble_35"),              duration: 8000 },
  { name: "meditate",   spotKey: "meditation",  bubble: t("page.manor_office.idle_action_bubble_36"),  duration: 7000 },
  { name: "yoga",       spotKey: "yoga",        bubble: t("page.manor_office.idle_action_bubble_37"),        duration: 6000 },
  { name: "chat",       spotKey: "",            bubble: t("page.manor_office.idle_action_bubble_38"),    duration: 4000 },
  { name: "phone",      spotKey: "",            bubble: t("page.manor_office.idle_action_bubble_39"),          duration: 5000 },
  { name: "wander",     spotKey: "",            bubble: t("page.manor_office.idle_action_bubble_40"),       duration: 4000 },
  { name: "wander",     spotKey: "",            bubble: t("page.manor_office.idle_action_bubble_41"),    duration: 3500 },
  { name: "wander",     spotKey: "",            bubble: t("page.manor_office.idle_action_bubble_42"),         duration: 4500 },
];
const WORKING_ACTION_DEFS: AgentActionDef[] = [
  { name: "coffee",     spotKey: "coffee",      bubble: t("page.manor_office.working_action_bubble_01"),        duration: 4000 },
  { name: "coffee",     spotKey: "coffee",      bubble: t("page.manor_office.working_action_bubble_02"),       duration: 3500 },
  { name: "server",     spotKey: "server",      bubble: t("page.manor_office.working_action_bubble_03"),   duration: 5000 },
  { name: "server",     spotKey: "server",      bubble: t("page.manor_office.working_action_bubble_04"),     duration: 4000 },
  { name: "whiteboard", spotKey: "whiteboard",  bubble: t("page.manor_office.working_action_bubble_05"),        duration: 4000 },
  { name: "whiteboard", spotKey: "whiteboard",  bubble: t("page.manor_office.working_action_bubble_06"),      duration: 5000 },
  { name: "water",      spotKey: "waterCooler", bubble: t("page.manor_office.working_action_bubble_07"),     duration: 3000 },
  { name: "snack",      spotKey: "snack",       bubble: t("page.manor_office.working_action_bubble_08"),       duration: 3000 },
  { name: "fridge",     spotKey: "fridge",       bubble: t("page.manor_office.working_action_bubble_09"),        duration: 2500 },
  { name: "printer",    spotKey: "printer",     bubble: t("page.manor_office.working_action_bubble_10"),    duration: 4500 },
  { name: "phone",      spotKey: "",            bubble: t("page.manor_office.working_action_bubble_11"),    duration: 4000 },
  { name: "phone",      spotKey: "",            bubble: t("page.manor_office.working_action_bubble_12"),     duration: 3500 },
];
type CatActionDef = { name: string; spotKey?: string; bubble: string; speed: number };
const CAT_ACTION_DEFS: CatActionDef[] = [
  { name: "sleep",   bubble: t("page.manor_office.cat_action_bubble_01"),              speed: 0 },
  { name: "sleep",   bubble: t("page.manor_office.cat_action_bubble_02"),              speed: 0 },
  { name: "sleep",   bubble: t("page.manor_office.cat_action_bubble_03"),            speed: 0 },
  { name: "wander",  bubble: "",                     speed: 0.4 },
  { name: "wander",  bubble: "",                     speed: 0.4 },
  { name: "wander",  bubble: t("page.manor_office.cat_action_bubble_04"),          speed: 0.4 },
  { name: "wander",  bubble: t("page.manor_office.cat_action_bubble_05"),    speed: 0.3 },
  { name: "trot",    bubble: "",                     speed: 0.7 },
  { name: "trot",    bubble: t("page.manor_office.cat_action_bubble_06"),            speed: 0.9 },
  { name: "desk",    bubble: t("page.manor_office.cat_action_bubble_07"),   speed: 0.5 },
  { name: "desk",    bubble: t("page.manor_office.cat_action_bubble_08"),     speed: 0.5 },
  { name: "sofa",    spotKey: "sofa",     bubble: t("page.manor_office.cat_action_bubble_09"),      speed: 0.4 },
  { name: "sofa",    spotKey: "sofa",     bubble: t("page.manor_office.cat_action_bubble_10"), speed: 0.3 },
  { name: "follow",  bubble: "",                     speed: 0.4 },
  { name: "follow",  bubble: t("page.manor_office.cat_action_bubble_11"),    speed: 0.4 },
  { name: "plant",   spotKey: "plant2",   bubble: t("page.manor_office.cat_action_bubble_12"),      speed: 0.4 },
  { name: "plant",   spotKey: "plant2",   bubble: t("page.manor_office.cat_action_bubble_13"),     speed: 0.4 },
  { name: "stare",   bubble: t("page.manor_office.cat_action_bubble_14"),              speed: 0 },
  { name: "stare",   bubble: t("page.manor_office.cat_action_bubble_15"),             speed: 0 },
  { name: "stare",   bubble: t("page.manor_office.cat_action_bubble_16"),            speed: 0 },
  { name: "window",  spotKey: "window",   bubble: t("page.manor_office.cat_action_bubble_17"),   speed: 0.4 },
  { name: "window",  spotKey: "window",   bubble: t("page.manor_office.cat_action_bubble_18"), speed: 0.4 },
  { name: "groom",   bubble: t("page.manor_office.cat_action_bubble_19"),           speed: 0 },
  { name: "groom",   bubble: t("page.manor_office.cat_action_bubble_20"),      speed: 0 },
  { name: "stretch", bubble: t("page.manor_office.cat_action_bubble_21"),            speed: 0 },
  { name: "stretch", bubble: t("page.manor_office.cat_action_bubble_22"),            speed: 0 },
  { name: "sofa",    spotKey: "fountain", bubble: t("page.manor_office.cat_action_bubble_23"),     speed: 0.4 },
  { name: "plant",   spotKey: "fishTank", bubble: t("page.manor_office.cat_action_bubble_24"),   speed: 0.4 },
  { name: "desk",    bubble: t("page.manor_office.cat_action_bubble_25"),  speed: 0.7 },
  { name: "wander",  bubble: t("page.manor_office.cat_action_bubble_26"),        speed: 0.5 },
];

function hashStr(s: string) { return s.split("").reduce((a, c) => a + c.charCodeAt(0), 0); }
function humanize(k: string) {
  return k.replace(/[_-]+/g, " ").trim().split(/\s+/)
    .map(w => w[0]?.toUpperCase() + w.slice(1).toLowerCase()).join(" ");
}

interface AgentData { id: string; name: string; charIdx: number; status: "working" | "idle"; tx: number; ty: number; }
interface RoomData {
  workspaceId: string; name: string; status: string;
  agents: AgentData[];
  goals: { label: string; progress: number }[];
  tasks: { status: string; count: number }[];
  totalTasks: number; totalDocs: number;
  themeIdx: number;
}

// ── CDN Loader ───────────────────────────────────────────────────────
const PHASER_CDNS = [
  "/assets/office/phaser.min.js",
  "https://cdn.jsdelivr.net/npm/phaser@3.80.1/dist/phaser.min.js",
  "https://unpkg.com/phaser@3.80.1/dist/phaser.min.js",
];
let phaserPromise: Promise<any> | null = null;
function loadPhaser(): Promise<any> {
  if ((window as any).Phaser) return Promise.resolve((window as any).Phaser);
  if (phaserPromise) return phaserPromise;
  phaserPromise = tryLoadScript(0);
  return phaserPromise;
}
function tryLoadScript(idx: number): Promise<any> {
  if (idx >= PHASER_CDNS.length) return Promise.reject(new Error("Failed to load Phaser from all sources"));
  return new Promise<any>((resolve, reject) => {
    const s = document.createElement("script");
    s.src = PHASER_CDNS[idx];
    s.onload = () => {
      if ((window as any).Phaser) resolve((window as any).Phaser);
      else reject(new Error("Phaser not on window"));
    };
    s.onerror = () => reject(new Error(`Failed: ${PHASER_CDNS[idx]}`));
    document.head.appendChild(s);
  }).catch(() => tryLoadScript(idx + 1));
}

// ══════════════════════════════════════════════════════════════════════
//  TOWN SCENE — 2D pixel-art manor town, buildings = workspaces
// ══════════════════════════════════════════════════════════════════════
const V_TILE = 16; // village tileset base tile size
const V_SCALE = 1.5; // smaller tiles — village feels bigger
const V_STEP = V_TILE * V_SCALE; // 24px
const V_BUILDING_BASES = ['v_house1', 'v_house2', 'v_church'] as const;
// 20 building variants: base sprite + tint color for variety
const V_BUILDINGS: { key: string; tint: number | null }[] = [
  { key: 'v_house1', tint: null },           // 0  original blue-roof cottage
  { key: 'v_house2', tint: null },           // 1  original orange-roof house
  { key: 'v_church', tint: null },           // 2  original teal church
  { key: 'v_house1', tint: 0xffddbb },      // 3  warm cottage
  { key: 'v_house2', tint: 0xddffdd },      // 4  green-tinted house
  { key: 'v_house1', tint: 0xddddff },      // 5  cool blue cottage
  { key: 'v_house2', tint: 0xffeedd },      // 6  peach house
  { key: 'v_church', tint: 0xfff0dd },      // 7  golden church
  { key: 'v_house1', tint: 0xffe8e8 },      // 8  pink cottage
  { key: 'v_house2', tint: 0xe8ffe8 },      // 9  mint house
  { key: 'v_house1', tint: 0xfff8dd },      // 10 sunny cottage
  { key: 'v_house2', tint: 0xddeeff },      // 11 ice-blue house
  { key: 'v_church', tint: 0xeeddff },      // 12 lavender church
  { key: 'v_house1', tint: 0xffe0cc },      // 13 coral cottage
  { key: 'v_house2', tint: 0xddf0dd },      // 14 sage house
  { key: 'v_house1', tint: 0xeeeeff },      // 15 silver cottage
  { key: 'v_house2', tint: 0xfff0e0 },      // 16 cream house
  { key: 'v_church', tint: 0xddfff0 },      // 17 jade church
  { key: 'v_house1', tint: 0xf0e0ff },      // 18 lilac cottage
  { key: 'v_house2', tint: 0xffeef0 },      // 19 rose house
];
const V_TREES = ['v_tree1', 'v_tree2', 'v_tree3'] as const;
const V_GRASS_DETAILS = ['v_grass_d1', 'v_grass_d2', 'v_grass_d3', 'v_grass_d4', 'v_grass_d5', 'v_grass_d6'] as const;
const V_GROUND_DETAILS = ['v_ground_d1', 'v_ground_d2', 'v_ground_d3', 'v_ground_d4', 'v_ground_d5'] as const;
const V_TERRAINS = ['v_terrain1', 'v_terrain2', 'v_terrain3', 'v_terrain5'] as const;

// Organic village — 20 buildings spread across a large landscape
const VILLAGE_PLOTS: { x: number; y: number; bldg: number }[] = [
  // Hilltop cluster (north)
  { x: 340, y: 180, bldg: 2 },   // church on the hill
  { x: 520, y: 220, bldg: 0 },   // cottage near church
  { x: 170, y: 260, bldg: 1 },   // large house
  { x: 680, y: 190, bldg: 3 },   // warm cottage

  // Village center band
  { x: 730, y: 370, bldg: 4 },   // green house at crossroads
  { x: 480, y: 400, bldg: 5 },   // cool cottage
  { x: 260, y: 440, bldg: 6 },   // peach house
  { x: 560, y: 320, bldg: 7 },   // golden church

  // Riverside (east)
  { x: 1060, y: 280, bldg: 8 },  // pink cottage by river
  { x: 1010, y: 500, bldg: 9 },  // mint riverside house
  { x: 1080, y: 680, bldg: 10 }, // sunny cottage

  // Western hamlet
  { x: 100, y: 500, bldg: 11 },  // ice-blue house
  { x: 120, y: 680, bldg: 12 },  // lavender church

  // Southern farms & settlement
  { x: 300, y: 650, bldg: 13 },  // coral cottage
  { x: 480, y: 700, bldg: 14 },  // sage house
  { x: 680, y: 660, bldg: 15 },  // silver cottage
  { x: 870, y: 710, bldg: 16 },  // cream house

  // Scattered outskirts
  { x: 900, y: 240, bldg: 17 },  // jade church northeast
  { x: 380, y: 540, bldg: 18 },  // lilac cottage mid
  { x: 820, y: 520, bldg: 19 },  // rose house mid-east
];

// Helper: generate winding path waypoints between two points
function windingPath(x0: number, y0: number, x1: number, y1: number, amplitude = 20, freq = 0.015): { x: number; y: number }[] {
  const pts: { x: number; y: number }[] = [];
  const dx = x1 - x0, dy = y1 - y0;
  const len = Math.sqrt(dx * dx + dy * dy);
  const steps = Math.ceil(len / V_STEP);
  for (let i = 0; i <= steps; i++) {
    const t = i / steps;
    const wobble = Math.sin(t * len * freq) * amplitude;
    // perpendicular offset
    const nx = -dy / len, ny = dx / len;
    pts.push({ x: x0 + dx * t + nx * wobble, y: y0 + dy * t + ny * wobble });
  }
  return pts;
}

// Hills — elevated terrain patches with terrain set textures
const VILLAGE_HILLS: { x: number; y: number; w: number; h: number; terrain: number }[] = [
  { x: 280, y: 160, w: 200, h: 120, terrain: 0 },  // north hill (church hill)
  { x: 80,  y: 500, w: 160, h: 100, terrain: 1 },   // western hillock
  { x: 820, y: 150, w: 140, h: 90,  terrain: 2 },   // northeast hill
  { x: 600, y: 600, w: 180, h: 110, terrain: 3 },   // southern rise
  { x: 1100, y: 650, w: 120, h: 80, terrain: 0 },   // southeast mound
  { x: 450, y: 820, w: 150, h: 90,  terrain: 1 },   // south-central hill
  { x: 1050, y: 120, w: 100, h: 70, terrain: 2 },   // far northeast knoll
  { x: 180, y: 780, w: 130, h: 85,  terrain: 3 },   // southwest mound
];


// All office sprites (loaded by town so office can reuse from cache)
const OFFICE_SPRITES = [
  "monitor", "monitor_off", "coffee", "sofa", "cat", "server", "plant",
  "whiteboard", "poster", "clock", "chair", "lamp", "rug", "water_cooler", "door",
  "filing_cabinet", "trash_can", "coffee_cup", "paper_stack", "picture_frame",
  "bulletin_board", "small_plant", "keyboard", "round_table", "vending_machine",
  "coat_rack", "phone", "ac_unit", "exit_sign", "fire_extinguisher",
  "garden_tree", "garden_bush", "flower_bed", "garden_fence", "stone_path",
  "fountain", "garden_lamp", "bench", "patio_umbrella", "bird_bath",
  "grass_patch", "pond", "hammock", "cactus",
  "couch_l", "loveseat", "beanbag", "recliner",
  "standing_desk", "bookshelf", "yoga_mat", "ping_pong", "arcade_machine",
  "pool_table", "treadmill", "rug_large", "rug_modern", "ceiling_fan",
  "reception_desk", "shadow", "steam",
  "trophy_case", "globe", "telescope", "fish_tank", "dartboard",
  "mini_fridge", "umbrella_stand", "marker_set", "beanbag_green",
  "neon_sign", "snack_bowl", "wall_shelf", "projector_screen",
  "guitar", "desk_lamp", "potted_flower", "pizza_box", "easel",
  "planter_box", "meditation_cushion", "speaker", "foosball",
  "clock_modern", "bamboo_divider",
];


function createTownScene(Phaser: any) {
  return class TownScene extends Phaser.Scene {
    rooms: RoomData[] = [];
    plots: { x: number; y: number; room: RoomData | null; sprite: any; container: any }[] = [];
    townNPCs: any[] = [];
    isDragging = false;
    dragStart = { x: 0, y: 0 };
    dragMoved = false;

    constructor() { super({ key: "TownScene" }); }
    init(data: any) { if (data?.rooms) this.rooms = data.rooms; }

    preload() {
      for (const key of CHAR_KEYS) {
        if (!this.textures.exists(key))
          this.load.spritesheet(key, `/assets/office/${key}.png`, { frameWidth: CHAR_FRAME_W, frameHeight: CHAR_FRAME_H });
      }
      // Village tileset (pixeljad)
      const villageAssets = [
        'v_grass', 'v_ground', 'v_water',
        ...V_BUILDING_BASES, ...V_TREES,
        ...V_GRASS_DETAILS, ...V_GROUND_DETAILS,
        'v_water_d1', 'v_water_d2', 'v_water_d3', 'v_water_d4', 'v_water_d5',
        'v_fence1', 'v_fence2', 'v_bridge', 'v_pit', 'v_stairs',
        'v_terrain1', 'v_terrain2', 'v_terrain3', 'v_terrain4', 'v_terrain5',
        'v_terrain3c', 'v_terrain4c',
      ];
      for (const s of villageAssets) {
        if (!this.textures.exists(s)) this.load.image(s, `/assets/office/${s}.png`);
      }
      for (const s of OFFICE_SPRITES) {
        if (!this.textures.exists(s)) this.load.image(s, `/assets/office/${s}.png`);
      }
      for (const s of ["floors", "furniture", "walls_sheet", "windows_sheet", "interior_sheet"]) {
        if (!this.textures.exists(s)) this.load.image(s, `/assets/office/${s.replace("_sheet", "")}.png`);
      }
      // Pixel Life office tiles
      for (const s of ["office_floor_stone", "office_wall_brick", "office_wall_plain", "office_wall_bottom"]) {
        if (!this.textures.exists(s)) this.load.image(s, `/assets/office/${s}.png`);
      }
    }

    create() {
      // Set NEAREST filter on all image textures so pixel art stays crisp
      this.textures.each((tex: any) => { if (tex.key !== '__DEFAULT' && tex.key !== '__MISSING') tex.setFilter(Phaser.Textures.FilterMode.NEAREST); });

      const cam = this.cameras.main;
      cam.setBackgroundColor(0x4a8a2a);

      const worldW = 1500;
      const worldH = 1000;
      cam.setBounds(0, 0, worldW, worldH);
      cam.setZoom(0.65); // zoomed out — big expansive village

      // ── GRASS GROUND ──
      for (let gy = 0; gy < worldH; gy += V_STEP) {
        for (let gx = 0; gx < worldW; gx += V_STEP) {
          this.add.image(gx + V_STEP / 2, gy + V_STEP / 2, 'v_grass').setScale(V_SCALE).setDepth(0);
          const hash = ((gx * 7 + gy * 13) >>> 3) % 20;
          if (hash < 5) {
            this.add.image(gx + V_STEP / 2, gy + V_STEP / 2, V_GRASS_DETAILS[hash % V_GRASS_DETAILS.length]).setScale(V_SCALE).setDepth(0.5);
          }
        }
      }

      // ── HILLS ── (elevated terrain patches)
      this.drawHills();

      // ── WINDING PATHS ──
      this.drawPaths();

      // ── RIVER ──
      this.drawRiver(worldW, worldH);

      // ── PONDS ──
      this.drawPond(160, 780, 5, 4);
      this.drawPond(920, 850, 4, 3);

      // ── VILLAGE SIGN ──
      const signG = this.add.graphics();
      signG.fillStyle(0x5a3a1a, 0.95);
      signG.fillRoundedRect(worldW / 2 - 70, 15, 140, 28, 5);
      signG.lineStyle(1.5, 0x8a6a3a, 0.8);
      signG.strokeRoundedRect(worldW / 2 - 70, 15, 140, 28, 5);
      signG.setDepth(900);
      this.add.text(worldW / 2, 29, "MANOR VILLAGE", {
        fontFamily: "'Courier New', monospace", fontSize: "14px",
        color: "#f0d890", fontStyle: "bold",
      }).setOrigin(0.5).setDepth(901);

      // ── BUILDINGS ──
      this.plots = [];
      for (let i = 0; i < VILLAGE_PLOTS.length; i++) {
        const vp = VILLAGE_PLOTS[i];
        const room = i < this.rooms.length ? this.rooms[i] : null;
        this.placePlot(vp.x, vp.y, room, i, vp.bldg);
      }

      // ── DECORATIONS ──
      this.addDecor(worldW, worldH);

      // ── NPCs ──
      this.spawnNPCs(worldW, worldH);

      // ── CAMERA DRAG ──
      this.input.on("pointerdown", (p: any) => {
        this.isDragging = true;
        this.dragMoved = false;
        this.dragStart = { x: p.x + cam.scrollX, y: p.y + cam.scrollY };
      });
      this.input.on("pointermove", (p: any) => {
        if (!this.isDragging) return;
        const dx = this.dragStart.x - p.x - cam.scrollX;
        const dy = this.dragStart.y - p.y - cam.scrollY;
        if (Math.abs(dx) > 3 || Math.abs(dy) > 3) this.dragMoved = true;
        cam.scrollX = this.dragStart.x - p.x;
        cam.scrollY = this.dragStart.y - p.y;
      });
      this.input.on("pointerup", () => { this.isDragging = false; });

      // Center on village
      cam.scrollX = Math.max(0, (worldW - GW / cam.zoom) / 2);
      cam.scrollY = Math.max(0, (worldH - GH / cam.zoom) / 2);
      this.game.events.emit("sceneInfo", { scene: "town", rooms: this.rooms });
    }

    drawHills() {
      for (const hill of VILLAGE_HILLS) {
        const tk = V_TERRAINS[hill.terrain];
        // Place terrain set image as the hill core
        this.add.image(hill.x + hill.w / 2, hill.y + hill.h / 2, tk).setScale(V_SCALE * 1.2).setDepth(0.8);
        // Tint the grass around the hill slightly darker/greener for elevation feel
        const gfx = this.add.graphics().setDepth(0.3);
        gfx.fillStyle(0x2a6a12, 0.15);
        gfx.fillEllipse(hill.x + hill.w / 2, hill.y + hill.h / 2, hill.w * 1.3, hill.h * 1.3);
        // Subtle shadow on south side for depth
        const shd = this.add.graphics().setDepth(0.4);
        shd.fillStyle(0x000000, 0.06);
        shd.fillEllipse(hill.x + hill.w / 2, hill.y + hill.h * 0.8, hill.w * 1.1, hill.h * 0.4);
      }
    }

    drawPaths() {
      // Build winding paths connecting key locations
      const connections: [number, number, number, number, number?, number?][] = [
        // [x0, y0, x1, y1, amplitude, freq]
        // Main road: west to east across village center
        [50, 420, 1100, 400, 25, 0.010],
        // North road: hilltop area
        [80, 240, 900, 230, 18, 0.012],
        // South road
        [80, 700, 1080, 700, 20, 0.008],
        // North to center connectors
        [180, 260, 260, 440, 10, 0.020],
        [540, 230, 560, 330, 8, 0.022],
        [690, 200, 730, 370, 12, 0.015],
        // Center to south connectors
        [260, 450, 300, 650, 15, 0.012],
        [490, 410, 480, 700, 12, 0.014],
        [730, 380, 690, 660, 10, 0.016],
        // Riverside paths
        [900, 240, 1060, 280, 8, 0.020],
        [830, 400, 1010, 500, 10, 0.018],
        [870, 710, 1080, 680, 8, 0.020],
        // Western hamlet paths
        [100, 440, 100, 680, 12, 0.015],
        // Mid connectors
        [380, 540, 820, 520, 15, 0.012],
        // Southern cross paths
        [300, 660, 680, 660, 12, 0.014],
        [480, 710, 870, 720, 10, 0.012],
        // Pond approach
        [100, 700, 160, 780, 6, 0.025],
        // Village square loop
        [400, 350, 600, 350, 8, 0.020],
        [600, 350, 650, 420, 6, 0.025],
        // Far south connector
        [120, 700, 480, 850, 18, 0.008],
        [680, 670, 920, 850, 12, 0.010],
      ];

      const half = 1; // path width: 3 tiles
      for (const [x0, y0, x1, y1, amp, frq] of connections) {
        const pts = windingPath(x0, y0, x1, y1, amp || 20, frq || 0.015);
        for (const pt of pts) {
          for (let dx = -half; dx <= half; dx++) {
            for (let dy = -half; dy <= half; dy++) {
              // Rounded edges: skip corners for organic look
              if (Math.abs(dx) === half && Math.abs(dy) === half) continue;
              const px = pt.x + dx * V_STEP;
              const py = pt.y + dy * V_STEP;
              this.add.image(px, py, 'v_ground').setScale(V_SCALE).setDepth(1);
              const hash = ((px * 11 + py * 17) >>> 3) % 12;
              if (hash < 2) {
                this.add.image(px, py, V_GROUND_DETAILS[hash]).setScale(V_SCALE).setDepth(1.5);
              }
            }
          }
        }
      }
    }

    drawRiver(worldW: number, worldH: number) {
      // River meanders through the east side
      const baseX = 1200;
      const riverW = 5;
      for (let y = 0; y < worldH; y += V_STEP) {
        const wobble = Math.sin(y * 0.006) * 50 + Math.sin(y * 0.015) * 20;
        for (let dx = 0; dx < riverW; dx++) {
          const wx = baseX + wobble + dx * V_STEP;
          if (wx < 0 || wx > worldW + 50) continue;
          this.add.image(wx, y + V_STEP / 2, 'v_water').setScale(V_SCALE).setDepth(1);
          const hash = ((wx * 13 + y * 7) >>> 3) % 14;
          if (hash < 3) {
            this.add.image(wx, y + V_STEP / 2, `v_water_d${(hash % 5) + 1}`).setScale(V_SCALE).setDepth(1.5);
          }
        }
      }
      // Bridge
      const bridgeY = 400;
      const bridgeWobble = Math.sin(bridgeY * 0.006) * 50 + Math.sin(bridgeY * 0.015) * 20;
      this.add.image(baseX + bridgeWobble + (riverW * V_STEP) / 2, bridgeY, 'v_bridge').setScale(V_SCALE).setDepth(bridgeY + 5);
    }

    drawPond(cx: number, cy: number, pw: number, ph: number) {
      for (let dy = 0; dy < ph; dy++) {
        for (let dx = 0; dx < pw; dx++) {
          // Elliptical shape
          const nx = (dx - pw / 2 + 0.5) / (pw / 2);
          const ny = (dy - ph / 2 + 0.5) / (ph / 2);
          if (nx * nx + ny * ny > 1) continue;
          const px = cx + dx * V_STEP;
          const py = cy + dy * V_STEP;
          this.add.image(px, py, 'v_water').setScale(V_SCALE).setDepth(1);
          const hash = ((px * 7 + py * 11) >>> 3) % 10;
          if (hash < 2) {
            this.add.image(px, py, `v_water_d${(hash % 5) + 1}`).setScale(V_SCALE).setDepth(1.5);
          }
        }
      }
    }

    placePlot(cx: number, cy: number, room: RoomData | null, index: number, bldgType: number) {
      const isActive = !!room;
      const container = this.add.container(cx, cy).setDepth(cy);
      const bldgScale = 1.2; // small buildings — expansive village

      const bldgDef = V_BUILDINGS[bldgType % V_BUILDINGS.length];
      const bldg = this.add.image(0, -10, bldgDef.key).setScale(bldgScale);
      if (!isActive) {
        bldg.setTint(0x556677);
        bldg.setAlpha(0.5);
      } else if (bldgDef.tint) {
        bldg.setTint(bldgDef.tint);
      }
      container.add(bldg);

      // Ground shadow
      const shadow = this.add.graphics();
      shadow.fillStyle(0x000000, 0.10);
      shadow.fillEllipse(0, bldg.displayHeight * 0.28, bldg.displayWidth * 0.75, 8);
      container.add(shadow);
      container.sendToBack(shadow);

      if (isActive && room) {
        const label = room.name.length > 14 ? room.name.slice(0, 13) + ".." : room.name;
        const signBg = this.add.graphics();
        const sw = Math.max(55, label.length * 6 + 14);
        const sy = bldg.displayHeight * 0.30 + 6;
        signBg.fillStyle(0x1e293b, 0.9);
        signBg.fillRoundedRect(-sw / 2, sy, sw, 16, 3);
        signBg.lineStyle(0.6, 0x3a5a7a, 0.5);
        signBg.strokeRoundedRect(-sw / 2, sy, sw, 16, 3);
        container.add(signBg);
        const nameText = this.add.text(0, sy + 7, label, {
          fontFamily: "'Courier New', monospace", fontSize: "9px",
          color: "#e8e0d0", fontStyle: "bold",
        }).setOrigin(0.5);
        container.add(nameText);

        if (room.agents.length > 0) {
          const bx = bldg.displayWidth / 2 - 2;
          const by = -bldg.displayHeight / 2 + 6;
          const badge = this.add.graphics();
          badge.fillStyle(0x10b981, 0.9);
          badge.fillCircle(bx, by, 6);
          badge.lineStyle(0.8, 0xffffff, 0.5);
          badge.strokeCircle(bx, by, 6);
          container.add(badge);
          container.add(this.add.text(bx, by, String(room.agents.length), {
            fontFamily: "monospace", fontSize: "8px", color: "#fff", fontStyle: "bold",
          }).setOrigin(0.5));
        }

        bldg.setInteractive({ useHandCursor: true });
        bldg.on("pointerover", () => {
          this.tweens.add({ targets: container, scaleX: 1.1, scaleY: 1.1, y: cy - 3, duration: 150, ease: "Sine.easeOut" });
          nameText.setColor("#ffe890");
        });
        bldg.on("pointerout", () => {
          this.tweens.add({ targets: container, scaleX: 1, scaleY: 1, y: cy, duration: 150, ease: "Sine.easeOut" });
          nameText.setColor("#e8e0d0");
        });
        bldg.on("pointerdown", () => {
          if (this.dragMoved) return;
          this.zoomToBuilding(container, room);
        });
      } else {
        const sy = bldg.displayHeight * 0.30 + 6;
        container.add(this.add.text(0, sy + 4, "FOR RENT", {
          fontFamily: "'Courier New', monospace", fontSize: "8px",
          color: "#5a5a6a", fontStyle: "bold",
        }).setOrigin(0.5));
      }

      this.plots.push({ x: cx, y: cy, room, sprite: bldg, container });
    }

    zoomToBuilding(container: any, room: RoomData) {
      this.plots.forEach(p => p.sprite.disableInteractive());
      const cam = this.cameras.main;
      cam.pan(container.x, container.y, 600, "Sine.easeInOut");
      cam.zoomTo(3, 600, "Sine.easeInOut");
      this.time.delayedCall(500, () => { cam.fadeOut(300, 26, 26, 46); });
      this.time.delayedCall(850, () => {
        this.scene.start("OfficeScene", { room, allRooms: this.rooms });
      });
    }

    addDecor(worldW: number, worldH: number) {
      const g = this.add.graphics();

      // Dense forest border (thick canopy around all edges)
      for (let i = 0; i < 70; i++) {
        const side = i % 4;
        let tx: number, ty: number;
        if (side === 0) { tx = Math.random() * worldW; ty = Math.random() * 70; }
        else if (side === 1) { tx = Math.random() * worldW; ty = worldH - Math.random() * 70; }
        else if (side === 2) { tx = Math.random() * 70; ty = 60 + Math.random() * (worldH - 120); }
        else { tx = worldW - Math.random() * 50; ty = 60 + Math.random() * (worldH - 120); }
        const tk = V_TREES[i % V_TREES.length];
        this.add.image(tx, ty, tk).setScale(1.4 + Math.random() * 0.7).setDepth(ty + 10);
      }

      // Tree clusters between buildings (groves)
      const groves = [
        { cx: 650, cy: 150, count: 6 },  // north grove
        { cx: 900, cy: 250, count: 5 },  // northeast
        { cx: 100, cy: 400, count: 4 },  // west side
        { cx: 600, cy: 550, count: 4 },  // center-south
        { cx: 850, cy: 600, count: 5 },  // south-east
        { cx: 350, cy: 550, count: 3 },  // mid
        { cx: 950, cy: 800, count: 5 },  // south border
        { cx: 500, cy: 850, count: 4 },  // bottom
        { cx: 200, cy: 340, count: 3 },  // west-center
        { cx: 750, cy: 460, count: 3 },  // mid-east
        { cx: 1050, cy: 400, count: 4 }, // east before river
        { cx: 400, cy: 100, count: 3 },  // far north
      ];
      groves.forEach((gr, gi) => {
        for (let t = 0; t < gr.count; t++) {
          const tx = gr.cx + (Math.random() - 0.5) * 90;
          const ty = gr.cy + (Math.random() - 0.5) * 65;
          const tk = V_TREES[(gi + t) % V_TREES.length];
          this.add.image(tx, ty, tk).setScale(1.2 + Math.random() * 0.6).setDepth(ty + 10);
        }
      });

      // Fences around building yards (every 3rd active building)
      this.plots.forEach((p, i) => {
        if (i % 3 === 0 && p.room) {
          const fk = i % 2 === 0 ? 'v_fence1' : 'v_fence2';
          for (let fx = -32; fx <= 32; fx += V_STEP) {
            this.add.image(p.x + fx, p.y + 60, fk).setScale(V_SCALE * 0.8).setDepth(p.y + 50);
          }
          // Side fences
          for (let fy = 20; fy <= 56; fy += V_STEP) {
            this.add.image(p.x - 36, p.y + fy, fk).setScale(V_SCALE * 0.8).setDepth(p.y + fy);
            this.add.image(p.x + 36, p.y + fy, fk).setScale(V_SCALE * 0.8).setDepth(p.y + fy);
          }
        }
      });

      // Well in village center area
      this.add.image(580, 410, 'v_pit').setScale(V_SCALE).setDepth(411);


      // ── LAMP POSTS along main roads ──
      const lampPositions = [
        { x: 200, y: 420 }, { x: 400, y: 410 }, { x: 650, y: 400 },
        { x: 850, y: 390 }, { x: 250, y: 240 }, { x: 500, y: 230 },
        { x: 200, y: 700 }, { x: 450, y: 700 }, { x: 700, y: 700 },
        { x: 950, y: 700 }, { x: 550, y: 330 }, { x: 750, y: 380 },
      ];
      lampPositions.forEach(lp => {
        const lg = this.add.graphics().setDepth(lp.y + 5);
        // Pole
        lg.fillStyle(0x4a4a4a); lg.fillRect(lp.x - 1, lp.y - 18, 3, 20);
        // Lamp head
        lg.fillStyle(0x6a6a6a); lg.fillRect(lp.x - 4, lp.y - 20, 9, 4);
        // Light glow
        lg.fillStyle(0xffee88, 0.15); lg.fillCircle(lp.x, lp.y - 14, 12);
        lg.fillStyle(0xffdd44, 0.25); lg.fillCircle(lp.x, lp.y - 18, 4);
      });

      // ── BENCHES along paths ──
      const benchPositions = [
        { x: 300, y: 424 }, { x: 750, y: 404 }, { x: 350, y: 244 },
        { x: 600, y: 234 }, { x: 300, y: 704 }, { x: 600, y: 704 },
        { x: 170, y: 460 }, { x: 450, y: 540 },
      ];
      benchPositions.forEach(bp => {
        const bg = this.add.graphics().setDepth(bp.y + 3);
        // Bench seat
        bg.fillStyle(0x7a5a2a); bg.fillRect(bp.x - 10, bp.y - 2, 20, 5);
        // Legs
        bg.fillStyle(0x5a3a1a); bg.fillRect(bp.x - 9, bp.y + 3, 3, 4); bg.fillRect(bp.x + 6, bp.y + 3, 3, 4);
        // Back rest
        bg.fillStyle(0x8a6a3a); bg.fillRect(bp.x - 10, bp.y - 6, 20, 3);
      });

      // ── FLOWER PATCHES — colorful clusters ──
      const flowerClusters = [
        { x: 160, y: 300, count: 8 }, { x: 430, y: 480, count: 6 },
        { x: 780, y: 300, count: 7 }, { x: 300, y: 800, count: 5 },
        { x: 620, y: 750, count: 6 }, { x: 900, y: 600, count: 5 },
        { x: 100, y: 620, count: 4 }, { x: 550, y: 180, count: 5 },
        { x: 440, y: 630, count: 4 }, { x: 830, y: 460, count: 5 },
      ];
      flowerClusters.forEach(fc => {
        const fg = this.add.graphics().setDepth(0.7);
        const colors = [0xff6688, 0xffaa44, 0xffee44, 0xaa66ff, 0xff4466, 0x66ccff, 0xff88cc];
        for (let f = 0; f < fc.count; f++) {
          const fx = fc.x + (Math.random() - 0.5) * 40;
          const fy = fc.y + (Math.random() - 0.5) * 30;
          fg.fillStyle(colors[f % colors.length], 0.9);
          fg.fillCircle(fx, fy, 2 + Math.random() * 2);
          // Stem
          fg.fillStyle(0x44aa22, 0.7);
          fg.fillRect(fx - 0.5, fy + 2, 1, 3 + Math.random() * 2);
        }
      });

      // ── ROCKS & BOULDERS ──
      const rocks = [
        { x: 710, y: 280, s: 6 }, { x: 200, y: 550, s: 5 }, { x: 860, y: 500, s: 4 },
        { x: 440, y: 760, s: 7 }, { x: 1020, y: 350, s: 5 }, { x: 300, y: 160, s: 4 },
        { x: 550, y: 460, s: 3 }, { x: 780, y: 780, s: 5 }, { x: 130, y: 710, s: 4 },
        { x: 950, y: 160, s: 5 }, { x: 640, y: 630, s: 4 },
      ];
      rocks.forEach(r => {
        const rg = this.add.graphics().setDepth(r.y + 2);
        rg.fillStyle(0x888888, 0.8); rg.fillEllipse(r.x, r.y, r.s * 2, r.s * 1.4);
        rg.fillStyle(0xaaaaaa, 0.4); rg.fillEllipse(r.x - 1, r.y - 1, r.s * 1.5, r.s);
        rg.fillStyle(0x000000, 0.08); rg.fillEllipse(r.x + 1, r.y + r.s * 0.5, r.s * 2, r.s * 0.5);
      });

      // ── GARDEN PLOTS near houses (vegetable patches) ──
      const gardens = [
        { x: 200, y: 300 }, { x: 530, y: 260 }, { x: 700, y: 220 },
        { x: 150, y: 530 }, { x: 500, y: 730 }, { x: 850, y: 740 },
      ];
      gardens.forEach(gd => {
        const gg = this.add.graphics().setDepth(1.2);
        // Soil patch
        gg.fillStyle(0x6a4a22, 0.5); gg.fillRect(gd.x - 15, gd.y, 30, 20);
        // Row furrows
        for (let r = 0; r < 3; r++) {
          gg.fillStyle(0x5a3a12, 0.4); gg.fillRect(gd.x - 13, gd.y + 3 + r * 7, 26, 2);
          // Crops (green sprouts)
          for (let c = 0; c < 4; c++) {
            gg.fillStyle(0x44aa22 + (c * 0x111100), 0.8);
            gg.fillCircle(gd.x - 10 + c * 7, gd.y + 2 + r * 7, 2);
          }
        }
      });

      // ── SIGNPOSTS at crossroads ──
      const signposts = [
        { x: 260, y: 440, labels: ["Market", "Church"] },
        { x: 730, y: 370, labels: ["River", "Village"] },
        { x: 490, y: 700, labels: ["Pond", "South"] },
        { x: 100, y: 680, labels: ["Hamlet", "North"] },
      ];
      signposts.forEach(sp => {
        const sg = this.add.graphics().setDepth(sp.y + 8);
        // Pole
        sg.fillStyle(0x6a4a2a); sg.fillRect(sp.x - 1, sp.y - 20, 3, 24);
        // Signs (pointing arrows)
        sg.fillStyle(0x8a6a3a); sg.fillRect(sp.x - 16, sp.y - 20, 33, 8);
        sg.fillStyle(0x7a5a2a); sg.fillRect(sp.x - 16, sp.y - 13, 33, 7);
        // Arrow tips
        sg.fillTriangle(sp.x + 17, sp.y - 16, sp.x + 23, sp.y - 16, sp.x + 17, sp.y - 12);
        sg.fillTriangle(sp.x - 16, sp.y - 9, sp.x - 22, sp.y - 9, sp.x - 16, sp.y - 13);
      });

      // ── Additional terrain patches (grass variety) ──
      for (let i = 0; i < 25; i++) {
        const gx = 80 + Math.random() * (worldW - 160);
        const gy = 60 + Math.random() * (worldH - 120);
        const gd = V_GRASS_DETAILS[i % V_GRASS_DETAILS.length];
        this.add.image(gx, gy, gd).setScale(V_SCALE * (1.2 + Math.random() * 0.8)).setDepth(0.6);
      }

    }

    spawnNPCs(worldW: number, worldH: number) {
      for (const key of CHAR_KEYS) {
        ["down", "right", "up", "left"].forEach((dir, col) => {
          const animKey = `${key}_walk_${dir}`;
          if (!this.anims.exists(animKey)) {
            this.anims.create({
              key: animKey,
              frames: [{ key, frame: col }, { key, frame: 4 + col }, { key, frame: col }, { key, frame: 8 + col }],
              frameRate: 5, repeat: -1,
            });
          }
        });
      }

      this.townNPCs = [];
      // NPCs walk along the main paths
      const npcRoutes = [
        { y: 420, xMin: 60,  xMax: 1150 },  // main road
        { y: 260, xMin: 100, xMax: 650 },   // north road
        { y: 710, xMin: 120, xMax: 1000 },  // south road
        { y: 350, xMin: 400, xMax: 700 },   // village square
        { y: 540, xMin: 380, xMax: 820 },   // mid connector
      ];
      const npcCount = Math.min(Math.max(this.rooms.length * 2, 6), 16);
      for (let i = 0; i < npcCount; i++) {
        const route = npcRoutes[i % npcRoutes.length];
        const sx = route.xMin + Math.random() * (route.xMax - route.xMin);
        const charKey = CHAR_KEYS[i % CHAR_KEYS.length];
        const sprite = this.add.sprite(sx, route.y + 3, charKey, 0).setScale(V_SCALE).setDepth(route.y + 20);
        const goRight = Math.random() > 0.5;
        sprite.play(`${charKey}_walk_${goRight ? "right" : "left"}`);
        this.townNPCs.push({ sprite, speed: 0.25 + Math.random() * 0.25, goRight, leftBound: route.xMin - 30, rightBound: route.xMax + 30 });
      }
    }

    update() {
      for (const npc of this.townNPCs) {
        npc.sprite.x += npc.goRight ? npc.speed : -npc.speed;
        if (npc.sprite.x > npc.rightBound) npc.sprite.x = npc.leftBound;
        if (npc.sprite.x < npc.leftBound) npc.sprite.x = npc.rightBound;
      }
    }
  };
}


// ══════════════════════════════════════════════════════════════════════
//  OFFICE SCENE — interior view of a single workspace
// ══════════════════════════════════════════════════════════════════════
function createOfficeScene(Phaser: any) {

  // --- Office Themes — each workspace gets a unique floor plan palette ---
  type TileRegion = { x: number; y: number; w: number; h: number };
  interface OfficeTheme {
    name: string;
    workTiles: [TileRegion, TileRegion];   // alternating work area floor
    loungeTiles: [TileRegion, TileRegion]; // alternating lounge floor
    corridorTile: TileRegion;              // corridor floor
    wallTop: [number, number, number];     // wall gradient top RGB
    wallBot: [number, number, number];     // wall gradient bottom RGB
    wallAccent: number;                    // crown molding / baseboard tint
    brassColor: number;                    // zone divider color
    ambientWork: number;                   // warm overlay in work area
    ambientLounge: number;                 // overlay in lounge area
    signBg: number;                        // company sign background
    signBorder: number;                    // company sign border
    floorTint: number;                     // tint for floor tiles
    wallTint: number;                      // tint for wall tiles
    loungeTint: number;                    // tint for lounge floor zone
  }
  const THEMES: OfficeTheme[] = [
    { // 0 — Classic: golden parquet + warm tile lounge
      name: "classic",
      workTiles:   [{ x: 160, y: 480, w: 32, h: 32 }, { x: 160, y: 512, w: 32, h: 32 }],
      loungeTiles: [{ x: 448, y: 544, w: 32, h: 32 }, { x: 448, y: 576, w: 32, h: 32 }],
      corridorTile: { x: 160, y: 768, w: 32, h: 32 },
      wallTop: [154, 126, 94], wallBot: [138, 110, 78],
      wallAccent: 0xd4b88c, brassColor: 0xc8a050,
      ambientWork: 0xfff0c0, ambientLounge: 0xd0e0f0,
      signBg: 0x2a1a0a, signBorder: 0x8a7a3a,
      floorTint: 0xe8d8c0, wallTint: 0xddccaa, loungeTint: 0xd0c8b0,
    },
    { // 1 — Executive: rich red-brown + deep emerald lounge
      name: "executive",
      workTiles:   [{ x: 416, y: 704, w: 32, h: 32 }, { x: 448, y: 704, w: 32, h: 32 }],
      loungeTiles: [{ x: 416, y: 672, w: 32, h: 32 }, { x: 448, y: 672, w: 32, h: 32 }],
      corridorTile: { x: 160, y: 800, w: 32, h: 32 },
      wallTop: [120, 80, 60], wallBot: [100, 64, 48],
      wallAccent: 0xc09060, brassColor: 0xb08040,
      ambientWork: 0xffe8c0, ambientLounge: 0xc0f0d0,
      signBg: 0x1a0a00, signBorder: 0x9a7a2a,
      floorTint: 0xc8a888, wallTint: 0xc0a080, loungeTint: 0xb0c8b0,
    },
    { // 2 — Modern: cool purple-gray + blue-gray lounge
      name: "modern",
      workTiles:   [{ x: 416, y: 736, w: 32, h: 32 }, { x: 448, y: 736, w: 32, h: 32 }],
      loungeTiles: [{ x: 416, y: 192, w: 32, h: 32 }, { x: 448, y: 192, w: 32, h: 32 }],
      corridorTile: { x: 160, y: 768, w: 32, h: 32 },
      wallTop: [110, 110, 130], wallBot: [90, 90, 110],
      wallAccent: 0xb0b0c8, brassColor: 0x8090b0,
      ambientWork: 0xe0e0f8, ambientLounge: 0xd0d8f0,
      signBg: 0x1a1a2e, signBorder: 0x5a5a8a,
      floorTint: 0xc8c8d8, wallTint: 0xb8b8cc, loungeTint: 0xc0c8d8,
    },
    { // 3 — Warm: terracotta + golden lounge
      name: "warm",
      workTiles:   [{ x: 416, y: 0, w: 32, h: 32 }, { x: 448, y: 0, w: 32, h: 32 }],
      loungeTiles: [{ x: 160, y: 480, w: 32, h: 32 }, { x: 160, y: 512, w: 32, h: 32 }],
      corridorTile: { x: 160, y: 800, w: 32, h: 32 },
      wallTop: [160, 120, 88], wallBot: [140, 100, 68],
      wallAccent: 0xe0c090, brassColor: 0xd0a060,
      ambientWork: 0xfff0d0, ambientLounge: 0xf0e8c0,
      signBg: 0x2a1808, signBorder: 0xa08030,
      floorTint: 0xe0c8a0, wallTint: 0xd8c098, loungeTint: 0xd8d0b0,
    },
    { // 4 — Garden: natural greens + earthy stone
      name: "garden",
      workTiles:   [{ x: 160, y: 480, w: 32, h: 32 }, { x: 160, y: 512, w: 32, h: 32 }],
      loungeTiles: [{ x: 416, y: 0, w: 32, h: 32 }, { x: 448, y: 0, w: 32, h: 32 }],
      corridorTile: { x: 160, y: 768, w: 32, h: 32 },
      wallTop: [130, 150, 110], wallBot: [110, 130, 90],
      wallAccent: 0xa0c090, brassColor: 0x80a060,
      ambientWork: 0xe0f0d0, ambientLounge: 0xd0e8c0,
      signBg: 0x1a2a10, signBorder: 0x6a8a3a,
      floorTint: 0xc0d0b0, wallTint: 0xb8c8a8, loungeTint: 0xb8c8a0,
    },
    { // 5 — Cozy: warm amber + soft cream
      name: "cozy",
      workTiles:   [{ x: 416, y: 704, w: 32, h: 32 }, { x: 448, y: 704, w: 32, h: 32 }],
      loungeTiles: [{ x: 416, y: 672, w: 32, h: 32 }, { x: 448, y: 672, w: 32, h: 32 }],
      corridorTile: { x: 160, y: 800, w: 32, h: 32 },
      wallTop: [170, 140, 100], wallBot: [150, 120, 80],
      wallAccent: 0xd8c0a0, brassColor: 0xc0a070,
      ambientWork: 0xfff4d8, ambientLounge: 0xf8e8d0,
      signBg: 0x2a1a08, signBorder: 0xb09040,
      floorTint: 0xe8d8b8, wallTint: 0xd8c8a8, loungeTint: 0xe0d0b8,
    },
    { // 6 — Playful: bright blue-purple
      name: "playful",
      workTiles:   [{ x: 416, y: 736, w: 32, h: 32 }, { x: 448, y: 736, w: 32, h: 32 }],
      loungeTiles: [{ x: 416, y: 192, w: 32, h: 32 }, { x: 448, y: 192, w: 32, h: 32 }],
      corridorTile: { x: 160, y: 768, w: 32, h: 32 },
      wallTop: [100, 100, 140], wallBot: [80, 80, 120],
      wallAccent: 0xa0a0d8, brassColor: 0x7080c0,
      ambientWork: 0xd8d8f8, ambientLounge: 0xe0d0f0,
      signBg: 0x1a1a30, signBorder: 0x6a6aaa,
      floorTint: 0xc8c0e0, wallTint: 0xb8b0d8, loungeTint: 0xd0c0e0,
    },
    { // 7 — Zen: soft sage green + sandy
      name: "zen",
      workTiles:   [{ x: 416, y: 0, w: 32, h: 32 }, { x: 448, y: 0, w: 32, h: 32 }],
      loungeTiles: [{ x: 160, y: 480, w: 32, h: 32 }, { x: 160, y: 512, w: 32, h: 32 }],
      corridorTile: { x: 160, y: 800, w: 32, h: 32 },
      wallTop: [140, 148, 130], wallBot: [120, 128, 110],
      wallAccent: 0xc0c8b0, brassColor: 0x90a080,
      ambientWork: 0xe8f0e0, ambientLounge: 0xf0ece0,
      signBg: 0x1a2018, signBorder: 0x7a8a60,
      floorTint: 0xd0d8c0, wallTint: 0xc8d0b8, loungeTint: 0xd8d8c0,
    },
  ];

  // Office layout zones (in tile coords: col, row)
  const WORK_AREA_MAX_COL = 18;  // cols 0-18 = work zone
  const CORRIDOR_MIN_ROW = 18;   // rows 18-19 = corridor

  // furniture.png (blonde wood) — 32x32 grid, 512x512
  // Desk: top portion at row 1 (y=32), col 3-4 area
  const FURN_DESK_TOP = { x: 128, y: 32, w: 64, h: 32 };
  const FURN_DESK_BOT = { x: 128, y: 64, w: 64, h: 32 };
  // Bookshelf: row 5-6, col 6-7
  const FURN_SHELF_TOP = { x: 192, y: 160, w: 64, h: 32 };
  const FURN_SHELF_BOT = { x: 192, y: 192, w: 64, h: 32 };
  // Chair: small, row 3, col 8
  const FURN_CHAIR = { x: 224, y: 128, w: 32, h: 32 };
  // Round table: row 3, col 4
  const FURN_TABLE = { x: 128, y: 96, w: 32, h: 32 };
  // Stool: row 3, col 7
  const FURN_STOOL = { x: 192, y: 128, w: 32, h: 32 };

  // interior.png — 512x512 — various items
  // Bookcase with books at approx (0, 128, 64, 96)
  const INT_BOOKCASE_TOP = { x: 0, y: 128, w: 64, h: 32 };
  const INT_BOOKCASE_MID = { x: 0, y: 160, w: 64, h: 32 };
  const INT_BOOKCASE_BOT = { x: 0, y: 192, w: 64, h: 32 };

  // windows.png — 512x512 — pick a nice window
  // White window with curtains: row 3, col 6 area (approx 320, 192)
  const WIN_LEFT = { x: 320, y: 192, w: 32, h: 64 };
  const WIN_RIGHT = { x: 352, y: 192, w: 32, h: 64 };

  class OfficeScene extends Phaser.Scene {
    roomData: RoomData | null = null;
    allRooms: RoomData[] = [];
    layout: OfficeLayout = OFFICE_LAYOUTS[0];
    agentSprites = new Map<string, any>();
    catContainer: any = null;
    catBubble: any = null;
    catTarget: { x: number; y: number } | null = null;
    catSpeed = 0.02;
    catAction: string = "sleep";
    catImg: any = null;
    catZzz: any = null;
    plaqueName: any = null;
    infoCard: any = null;

    constructor() { super({ key: "OfficeScene" }); }

    init(data: any) {
      if (data?.room) this.roomData = data.room;
      if (data?.allRooms) this.allRooms = data.allRooms;
    }

    preload() {
      // Sprites are shared across scenes via Phaser's texture cache.
      // TownScene preloads everything; only load if coming here directly.
      for (const key of CHAR_KEYS) {
        if (!this.textures.exists(key))
          this.load.spritesheet(key, `/assets/office/${key}.png`, { frameWidth: CHAR_FRAME_W, frameHeight: CHAR_FRAME_H });
      }
      for (const s of ["floors", "furniture", "walls_sheet", "windows_sheet", "interior_sheet"]) {
        if (!this.textures.exists(s)) this.load.image(s, `/assets/office/${s.replace("_sheet", "")}.png`);
      }
      for (const s of OFFICE_SPRITES) {
        if (!this.textures.exists(s)) this.load.image(s, `/assets/office/${s}.png`);
      }
      // Pixel Life office tiles
      for (const s of ["office_floor_stone", "office_wall_brick", "office_wall_plain", "office_wall_bottom"]) {
        if (!this.textures.exists(s)) this.load.image(s, `/assets/office/${s}.png`);
      }
    }

    create() {
      // Set NEAREST filter on all image textures so pixel art stays crisp
      this.textures.each((tex: any) => { if (tex.key !== '__DEFAULT' && tex.key !== '__MISSING') tex.setFilter(Phaser.Textures.FilterMode.NEAREST); });

      const cam = this.cameras.main;
      cam.setBackgroundColor(0x1a1a2e);
      cam.setZoom(0.8);
      // Center the office world — scrollX/Y define top-left, so center is at (scrollX + width/2, scrollY + height/2)
      // centerOn sets scrollX = x - width/2, then Phaser's zoom expands equally around that center point
      cam.centerOn(GW / 2, GH / 2 + 40);

      // Character sprite layout: 4 cols x 3 rows (16x17 per frame)
      // Columns = DIRECTIONS: col0=front, col1=right, col2=back, col3=left
      // Rows = POSES: row0=stand, row1=walk1, row2=walk2
      // Walk cycle for direction col: stand→walk1→stand→walk2
      for (const key of CHAR_KEYS) {
        const dirs = ["down", "right", "up", "left"] as const;
        dirs.forEach((dir, col) => {
          const animKey = `${key}_walk_${dir}`;
          if (!this.anims.exists(animKey)) {
            const stand = col;       // row0
            const walk1 = 4 + col;   // row1
            const walk2 = 8 + col;   // row2
            this.anims.create({
              key: animKey,
              frames: [{ key, frame: stand }, { key, frame: walk1 }, { key, frame: stand }, { key, frame: walk2 }],
              frameRate: 5,
              repeat: -1,
            });
          }
        });
      }

      // Generate tiny UI textures (shadow, status dots, steam particle)
      this.genUITextures();

      // Build room
      this.buildRoom();

      // Timers
      this.time.addEvent({ delay: 4000, loop: true, callback: () => this.blinkAgents() });
      this.time.addEvent({ delay: 7000, loop: true, callback: () => this.performCatAction() });
      this.time.addEvent({ delay: 9000, loop: true, callback: () => this.performIdleAgentActions() });
      this.time.addEvent({ delay: 22000, loop: true, callback: () => this.performWorkingAgentActions() });

      // Back to Town button (only if we came from the town)
      if (this.allRooms.length > 0) {
        // Place in world coords at top-left, above the wall
        const bx = 60, by = 16;
        const btnBg = this.add.graphics().setDepth(999);
        const drawBtn = (hover: boolean) => {
          btnBg.clear();
          btnBg.fillStyle(hover ? 0x2a3a50 : 0x1e293b, 0.92);
          btnBg.fillRoundedRect(bx - 50, by - 13, 100, 26, 8);
          btnBg.lineStyle(1, hover ? 0x5a8aba : 0x3a5a7a, hover ? 0.8 : 0.6);
          btnBg.strokeRoundedRect(bx - 50, by - 13, 100, 26, 8);
        };
        drawBtn(false);
        const btnLabel = this.add.text(bx, by, "\u2190 Town", {
          fontFamily: "'Courier New', monospace", fontSize: "15px",
          color: "#a0c0e0", fontStyle: "bold",
        }).setOrigin(0.5).setDepth(999);
        // Use a zone for reliable hit area
        const btnZone = this.add.zone(bx, by, 100, 26).setInteractive({ cursor: "pointer" }).setDepth(999);
        btnZone.on("pointerover", () => drawBtn(true));
        btnZone.on("pointerout", () => drawBtn(false));
        btnZone.on("pointerdown", () => {
          this.cameras.main.fadeOut(250, 18, 18, 30);
          this.time.delayedCall(300, () => {
            this.scene.start("TownScene", { rooms: this.allRooms });
          });
        });
      }

      // Fade in from town transition
      this.cameras.main.fadeIn(300, 18, 18, 30);

      // Emit scene info
      this.game.events.emit("sceneInfo", { scene: "office", room: this.roomData, rooms: this.allRooms });
    }

    update(time: number) {
      const AGENT_SPEED = 1.2; // pixels per frame
      this.agentSprites.forEach((s: any) => {
        const c = s.container;
        const t = c.getData("target");
        if (!t) return;
        const dx = t.x - c.x, dy = t.y - c.y;
        const dist = Math.sqrt(dx * dx + dy * dy);
        if (dist > 2) {
          const nx = dx / dist, ny = dy / dist;
          c.x += nx * AGENT_SPEED;
          c.y += ny * AGENT_SPEED;
          // Determine walk direction based on dominant axis
          const key = CHAR_KEYS[s.charIdx % CHAR_KEYS.length];
          let dir: string;
          if (Math.abs(dx) > Math.abs(dy)) {
            dir = dx > 0 ? "right" : "left";
          } else {
            dir = dy > 0 ? "down" : "up";
          }
          s.body.setFlipX(false); // no manual flip — the sprite has left/right cols
          const animKey = `${key}_walk_${dir}`;
          if (!s.walking || s.walkDir !== dir) {
            s.body.anims.play(animKey, true);
            s.walking = true;
            s.walkDir = dir;
          }
        } else if (s.walking) {
          // Arrived — stop walking, show static idle frame
          const key = CHAR_KEYS[s.charIdx % CHAR_KEYS.length];
          s.body.anims.stop();
          s.body.setFrame(0);
          s.body.setFlipX(false);
          s.walking = false;
          s.walkDir = null;
          // Context-appropriate arrival action
          if (s.doingAction === "coffee" || s.doingAction === "water" || s.doingAction === "snack" || s.doingAction === "fridge") {
            this.agentEmote(s, "sip");
          } else if (s.doingAction === "whiteboard" || s.doingAction === "server" || s.doingAction === "printer" || s.doingAction === "bookshelf") {
            this.agentEmote(s, "nod");
          } else if (s.doingAction === "sofa" || s.doingAction === "bench" || s.doingAction === "hammock") {
            this.agentEmote(s, "sit");
          } else if (s.doingAction === "meditate" || s.doingAction === "yoga") {
            this.agentEmote(s, "meditate");
          } else if (s.doingAction === "arcade" || s.doingAction === "pingpong" || s.doingAction === "foosball" || s.doingAction === "dartboard") {
            this.agentEmote(s, "celebrate");
          } else if (s.doingAction === "guitar") {
            this.agentEmote(s, "wave");
          } else if (s.doingAction === "fish" || s.doingAction === "trophy" || s.doingAction === "telescope") {
            this.agentEmote(s, "point");
          } else if (s.doingAction === "chat" || s.doingAction === "phone") {
            this.agentEmote(s, "wave");
          } else if (s.doingAction === "fountain") {
            this.agentEmote(s, "lean");
          }
        }
      });

      // Move cat — constant velocity, slow and smooth like a real cat
      if (this.catContainer && this.catTarget && this.catSpeed > 0) {
        const dx = this.catTarget.x - this.catContainer.x;
        const dy = this.catTarget.y - this.catContainer.y;
        const dist = Math.sqrt(dx * dx + dy * dy);
        if (dist > 2) {
          const nx = dx / dist, ny = dy / dist;
          this.catContainer.x += nx * this.catSpeed;
          this.catContainer.y += ny * this.catSpeed;
          if (this.catImg && Math.abs(dx) > 1) this.catImg.setFlipX(dx < 0);
          // Gentle body bob while walking
          if (this.catImg) this.catImg.y = Math.sin(time * 0.008) * 1;
        } else {
          this.catTarget = null;
          if (this.catImg) this.catImg.y = 0;
        }
      }
    }

    genUITextures() {
      // Shadow — softer elliptical
      this.tex("shadow", 24, 8, (g: any) => {
        g.fillStyle(0x000000, 0.12); g.fillEllipse(12, 4, 24, 8);
        g.fillStyle(0x000000, 0.08); g.fillEllipse(12, 4, 20, 6);
      });

      // Status dots with glow
      this.tex("dot_green", 10, 10, (g: any) => {
        g.fillStyle(0x10b981, 0.15); g.fillCircle(5, 5, 5);
        g.fillStyle(0x10b981); g.fillCircle(5, 5, 3.5);
        g.fillStyle(0x40f0b0, 0.6); g.fillCircle(4, 4, 1.5);
        g.lineStyle(1, 0xffffff, 0.7); g.strokeCircle(5, 5, 3.5);
      });
      this.tex("dot_yellow", 10, 10, (g: any) => {
        g.fillStyle(0xf59e0b, 0.15); g.fillCircle(5, 5, 5);
        g.fillStyle(0xf59e0b); g.fillCircle(5, 5, 3.5);
        g.fillStyle(0xffc040, 0.6); g.fillCircle(4, 4, 1.5);
        g.lineStyle(1, 0xffffff, 0.7); g.strokeCircle(5, 5, 3.5);
      });

      // Steam particle
      this.tex("steam", 4, 4, (g: any) => { g.fillStyle(0xffffff, 0.3); g.fillCircle(2, 2, 2); });
    }

    tex(key: string, w: number, h: number, fn: (g: any) => void) {
      if (this.textures.exists(key)) return;
      const g = this.add.graphics(); fn(g); g.generateTexture(key, w, h); g.destroy();
    }

    // ── Build Room — richly detailed office ──────────────────────────────
    buildRoom() {
      // === Pick theme & layout for this workspace ===
      const themeIdx = this.roomData?.themeIdx ?? 0;
      const theme = THEMES[themeIdx % THEMES.length];
      this.layout = OFFICE_LAYOUTS[themeIdx % OFFICE_LAYOUTS.length];
      // Floor — Pixel Life stone tiles with per-theme tinting & zone colors
      for (let row = 0; row < ROWS; row++) {
        for (let col = 0; col < COLS; col++) {
          const x = col * TILE + TILE / 2;
          const y = row * TILE + TILE / 2;
          if (row < 4) continue; // wall area
          const img = this.add.image(x, y, "office_floor_stone").setDisplaySize(TILE, TILE).setDepth(0);
          // Zone-based tinting: corridor, lounge (right side), work (left side)
          if (row >= CORRIDOR_MIN_ROW) {
            // Corridor — slightly darker
            img.setTint(Phaser.Display.Color.GetColor(
              ((theme.floorTint >> 16) & 0xff) * 0.85,
              ((theme.floorTint >> 8) & 0xff) * 0.85,
              (theme.floorTint & 0xff) * 0.85
            ));
          } else if (col > WORK_AREA_MAX_COL) {
            // Lounge zone
            img.setTint(theme.loungeTint);
          } else {
            // Work zone
            img.setTint(theme.floorTint);
          }
        }
      }

      // Floor border inlays — decorative strips between zones
      const floorDeco = this.add.graphics().setDepth(0.5);
      const divX = WORK_AREA_MAX_COL * TILE + TILE;
      floorDeco.fillStyle(0x000000, 0.15); floorDeco.fillRect(divX - 3, 132, 7, (CORRIDOR_MIN_ROW - 4) * TILE);
      floorDeco.fillStyle(theme.brassColor, 0.5); floorDeco.fillRect(divX - 1, 132, 3, (CORRIDOR_MIN_ROW - 4) * TILE);
      floorDeco.fillStyle(theme.brassColor, 0.25); floorDeco.fillRect(divX, 132, 1, (CORRIDOR_MIN_ROW - 4) * TILE);
      const corrY = CORRIDOR_MIN_ROW * TILE;
      floorDeco.fillStyle(0x000000, 0.1); floorDeco.fillRect(16, corrY + 2, GW - 32, 3);
      floorDeco.fillStyle(theme.brassColor, 0.4); floorDeco.fillRect(16, corrY - 2, GW - 32, 5);
      floorDeco.fillStyle(theme.brassColor, 0.5); floorDeco.fillRect(16, corrY - 1, GW - 32, 3);
      floorDeco.fillStyle(theme.brassColor, 0.3); floorDeco.fillRect(16, corrY, GW - 32, 1);
      // Floor scuff marks in high-traffic areas
      const scuffs = this.add.graphics().setDepth(0.4);
      scuffs.fillStyle(0x000000, 0.02);
      scuffs.fillEllipse(GW / 2, GH - 80, 100, 24);
      scuffs.fillEllipse(820, 310, 50, 16);
      scuffs.fillEllipse(320, 360, 120, 14);

      // === SKY BEHIND WINDOWS — per layout ===
      const sky = this.add.graphics().setDepth(0.8);
      for (const win of this.layout.windows) {
        for (let sy = 0; sy < 60; sy++) {
          const t = sy / 60;
          const r = Math.floor(100 + t * 40);
          const g = Math.floor(160 + t * 30);
          const b = Math.floor(220 - t * 20);
          sky.fillStyle((r << 16) | (g << 8) | b, 0.9);
          sky.fillRect(win.x + 4, 34 + sy, 72, 1);
        }
      }

      // === WALLS — Pixel Life wall tiles (matching preview layout) ===
      const accent = theme.wallAccent;
      // Back wall — all plain with wainscoting trim (as in preview)
      // Row 0: plain upper wall
      // Row 1: plain upper wall
      // Row 2: plain lower wall (below wainscoting)
      // Row 3: wall bottom (baseboard transition to floor)
      for (let col = 0; col < Math.ceil(GW / TILE); col++) {
        const x = col * TILE + TILE / 2;
        this.add.image(x, TILE * 0.5, "office_wall_plain").setDisplaySize(TILE, TILE).setDepth(1).setTint(theme.wallTint);
        this.add.image(x, TILE * 1.5, "office_wall_plain").setDisplaySize(TILE, TILE).setDepth(1).setTint(theme.wallTint);
        this.add.image(x, TILE * 2.5, "office_wall_bottom").setDisplaySize(TILE, TILE).setDepth(1).setTint(theme.wallTint);
        this.add.image(x, TILE * 3.5, "office_wall_bottom").setDisplaySize(TILE, TILE).setDepth(1).setTint(theme.wallTint);
      }
      const wall = this.add.graphics().setDepth(1.05);
      // Wainscoting trim — horizontal brown strip (as in preview)
      wall.fillStyle(0x6a5040); wall.fillRect(0, TILE * 2 - 2, GW, 4);
      wall.fillStyle(accent, 0.6); wall.fillRect(0, TILE * 2 - 1, GW, 2);
      // Small decorative nail dots on lower wall (as in preview)
      wall.fillStyle(accent, 0.4);
      for (let dx = TILE + 16; dx < GW - TILE; dx += TILE * 2) {
        wall.fillCircle(dx - 3, TILE * 2.8, 1.5);
        wall.fillCircle(dx + 3, TILE * 2.8, 1.5);
      }
      // Baseboard — dark trim at wall-floor transition
      wall.fillStyle(0x5a4030); wall.fillRect(0, 128, GW, 4);
      wall.fillStyle(accent, 0.25); wall.fillRect(0, 128, GW, 1);
      // Side walls — brick tiles (tinted per theme)
      for (let row = 0; row < Math.ceil(GH / TILE); row++) {
        const y = row * TILE + TILE / 2;
        this.add.image(TILE / 2, y, "office_wall_brick").setDisplaySize(TILE, TILE).setDepth(1).setTint(theme.wallTint);
        this.add.image(GW - TILE / 2, y, "office_wall_brick").setDisplaySize(TILE, TILE).setDepth(1).setTint(theme.wallTint);
      }
      // Bottom edge shadow
      wall.fillStyle(0x000000, 0.3); wall.fillRect(0, GH - 5, GW, 5);
      wall.fillStyle(accent, 0.15); wall.fillRect(0, GH - 5, GW, 1);

      // === WINDOWS — per layout ===
      for (const win of this.layout.windows) this.addRealWindow(win.x, win.y);

      // Window light beams (warm, layered) — per layout
      const beams = this.add.graphics().setDepth(1.1);
      for (const win of this.layout.windows) {
        const wx = win.x;
        beams.fillStyle(0xfff8c8, 0.04);
        beams.fillTriangle(wx, 132, wx + 80, 132, wx + 120, GH);
        beams.fillTriangle(wx, 132, wx + 80, 132, wx - 40, GH);
        beams.fillStyle(0xffffff, 0.02);
        beams.fillTriangle(wx + 20, 132, wx + 60, 132, wx + 80, GH);
        beams.fillTriangle(wx + 20, 132, wx + 60, 132, wx, GH);
        beams.fillStyle(0xfff0a0, 0.03);
        beams.fillEllipse(wx + 40, GH - 100, 160, 40);
      }

      // === DUST MOTES floating near windows ===
      this.tex("dust", 3, 3, (g: any) => { g.fillStyle(0xfff8d0, 0.5); g.fillCircle(1.5, 1.5, 1.5); });
      const wins = this.layout.windows;
      for (let di = 0; di < 20; di++) {
        const win = wins[di % wins.length];
        const baseX = win.x + Math.random() * 120;
        const baseY = 140 + Math.random() * 420;
        const mote = this.add.image(baseX, baseY, "dust").setDepth(999).setAlpha(0.15 + Math.random() * 0.2).setScale(0.5 + Math.random() * 0.8);
        this.tweens.add({
          targets: mote,
          x: mote.x + (Math.random() - 0.5) * 60,
          y: mote.y - 30 - Math.random() * 40,
          alpha: 0,
          duration: 6000 + Math.random() * 8000,
          repeat: -1,
          delay: Math.random() * 6000,
          onRepeat: () => { mote.x = baseX + (Math.random() - 0.5) * 30; mote.y = baseY + (Math.random() - 0.5) * 30; mote.setAlpha(0.15 + Math.random() * 0.2); },
        });
      }

      // === CEILING LIGHTS — per layout ===
      for (const lx of this.layout.ceilingLights) {
        const lg = this.add.graphics().setDepth(2);
        // Fixture mount
        lg.fillStyle(0xaaaaaa); lg.fillRect(lx - 2, 132, 4, 3);
        // Fixture body
        lg.fillStyle(0xcccccc); lg.fillRect(lx - 12, 135, 24, 3);
        lg.fillStyle(0xdddddd); lg.fillRect(lx - 10, 135, 20, 2);
        // Light bar (warm white)
        lg.fillStyle(0xfff4d8); lg.fillRoundedRect(lx - 10, 137, 20, 4, 1);
        lg.fillStyle(0xfff8e0, 0.8); lg.fillRoundedRect(lx - 8, 138, 16, 2, 1);
        // Light cone on floor
        const pool = this.add.graphics().setDepth(0.3);
        pool.fillStyle(0xfff8c8, 0.025);
        pool.fillTriangle(lx - 10, 141, lx + 10, 141, lx + 50, GH - 40);
        pool.fillTriangle(lx - 10, 141, lx + 10, 141, lx - 50, GH - 40);
        // Warm elliptical pool on floor
        pool.fillStyle(0xfff0a0, 0.02);
        pool.fillEllipse(lx, 440, 100, 30);
      }

      // === WALL DECORATIONS — from layout ===
      for (const item of this.layout.wallItems) {
        const dep = item.depth ?? (item.key === "ac_unit" ? 1.8 : 2);
        const img = this.add.image(item.x, item.y, item.key).setScale(item.scale).setDepth(dep);
        if (item.tint) img.setTint(item.tint);
        if (item.key === "neon_sign") {
          const ng = this.add.graphics().setDepth(dep - 0.1);
          ng.fillStyle(0xff6b9d, 0.06); ng.fillEllipse(item.x, item.y, 40, 18);
          this.tweens.add({ targets: ng, alpha: { from: 0.08, to: 0.01 }, duration: 2500, yoyo: true, repeat: -1 });
        }
        if (item.key === "dartboard") {
          const dg = this.add.graphics().setDepth(dep - 0.1);
          dg.fillStyle(0xfff0c0, 0.04); dg.fillEllipse(item.x, item.y, 28, 28);
        }
      }

      // Company sign — themed
      const signG = this.add.graphics().setDepth(2);
      signG.fillStyle(0x000000, 0.15); signG.fillRoundedRect(453, 45, 182, 30, 5);
      signG.fillStyle(theme.signBg); signG.fillRoundedRect(450, 42, 182, 30, 5);
      signG.fillStyle(theme.signBg, 0.8); signG.fillRoundedRect(451, 43, 180, 28, 4);
      signG.lineStyle(1.5, theme.signBorder); signG.strokeRoundedRect(451, 43, 180, 28, 4);
      signG.lineStyle(0.5, theme.brassColor, 0.4); signG.strokeRoundedRect(454, 46, 174, 22, 3);
      this.add.text(541, 57, "MANOR OFFICE", {
        fontFamily: "'Courier New', monospace", fontSize: "16px",
        color: "#f0d890", fontStyle: "bold", stroke: "#1a0a00", strokeThickness: 2,
      }).setOrigin(0.5).setDepth(3);

      // === FURNITURE FROM SPRITE SHEETS — from layout ===
      for (const si of this.layout.sheetItems) {
        this.addFurnitureFromSheet(si.x, si.y, si.sheet, si.sx, si.sy, si.sw, si.sh, si.scale, si.depth);
      }

      // === FLOOR ITEMS — from layout (data-driven with special effects) ===
      for (const item of this.layout.floorItems) {
        const d = item.depth ?? item.y;
        const img = this.add.image(item.x, item.y, item.key).setScale(item.scale).setDepth(d);
        if (item.tint) img.setTint(item.tint);
        // Special effects for specific furniture types
        this.addItemEffect(item, d);
      }

      // === CORRIDOR — universal elements ===
      this.add.image(GW / 2, GH - 32, "door").setScale(1.3).setDepth(1);
      const mat = this.add.graphics().setDepth(0.5);
      mat.fillStyle(0x5a4020, 0.6); mat.fillRoundedRect(GW / 2 - 30, GH - 55, 60, 20, 3);
      mat.fillStyle(0x6a5030, 0.4); mat.fillRoundedRect(GW / 2 - 28, GH - 53, 56, 16, 2);
      this.add.image(GW / 2, GH - 75, "exit_sign").setScale(1.6).setDepth(2);
      const exitGlow = this.add.graphics().setDepth(1.9);
      exitGlow.fillStyle(0x40c040, 0.06); exitGlow.fillEllipse(GW / 2, GH - 70, 50, 20);

      // === ATMOSPHERIC EFFECTS — themed ===
      const ambientGlow = this.add.graphics().setDepth(0.2);
      ambientGlow.fillStyle(theme.ambientWork, 0.015);
      ambientGlow.fillRect(16, 132, WORK_AREA_MAX_COL * TILE, (CORRIDOR_MIN_ROW - 4) * TILE);
      ambientGlow.fillStyle(theme.ambientLounge, 0.01);
      ambientGlow.fillRect(WORK_AREA_MAX_COL * TILE + 16, 132, GW - WORK_AREA_MAX_COL * TILE - 32, (CORRIDOR_MIN_ROW - 4) * TILE);

      // Vignette overlay — darker edges for depth
      const vignette = this.add.graphics().setDepth(998);
      // Top edge
      vignette.fillGradientStyle(0x000000, 0x000000, 0x000000, 0x000000, 0.15, 0.15, 0, 0);
      vignette.fillRect(0, 0, GW, 60);
      // Bottom edge
      vignette.fillGradientStyle(0x000000, 0x000000, 0x000000, 0x000000, 0, 0, 0.2, 0.2);
      vignette.fillRect(0, GH - 50, GW, 50);
      // Left edge
      vignette.fillGradientStyle(0x000000, 0x000000, 0x000000, 0x000000, 0.1, 0, 0.1, 0);
      vignette.fillRect(0, 0, 40, GH);
      // Right edge
      vignette.fillGradientStyle(0x000000, 0x000000, 0x000000, 0x000000, 0, 0.1, 0, 0.1);
      vignette.fillRect(GW - 40, 0, 40, GH);

      // === CAT (with actions!) ===
      this.catContainer = this.add.container(80, 560).setDepth(900);
      this.catImg = this.add.image(0, 0, "cat").setScale(1.8);
      const catShadow = this.add.image(0, 14, "shadow").setScale(1.2);
      const catName = this.add.text(0, 22, "Office Cat", {
        fontFamily: "'Courier New', monospace", fontSize: "13px",
        color: "#8b6914", stroke: "#ffffff", strokeThickness: 3, fontStyle: "bold",
      }).setOrigin(0.5);
      // Zzz indicator for sleeping
      this.catZzz = this.add.text(20, -18, "z z z", { fontFamily: "monospace", fontSize: "12px", color: "#8888aa" }).setAlpha(0.5);
      this.catContainer.add([catShadow, this.catImg, catName, this.catZzz]);
      this.catImg.setInteractive({ useHandCursor: true });
      this.catImg.on("pointerdown", () => {
        this.showCatActionBubble("Mrrp?");
        // Cat reacts to click — trots away
        const cwp = this.layout.catWanderPoints;
        const flee = cwp[Math.floor(Math.random() * cwp.length)];
        this.catTarget = flee;
        this.catSpeed = 0.8;
        this.catAction = "trot";
        if (this.catZzz) this.catZzz.setAlpha(0);
        if (this.catImg) this.tweens.add({ targets: this.catImg, scaleX: 1.8, scaleY: 1.8, duration: 200 });
      });
      // Subtle idle sway
      this.tweens.add({ targets: this.catImg, angle: { from: -2, to: 2 }, duration: 1800, yoyo: true, repeat: -1, ease: "Sine.easeInOut" });
      // Zzz float animation
      this.tweens.add({ targets: this.catZzz, y: { from: -18, to: -32 }, duration: 2000, yoyo: true, repeat: -1, ease: "Sine.easeInOut" });
      // Start first action after a short delay
      this.time.delayedCall(3000, () => this.performCatAction());

      // === BOTTOM PLAQUE — premium brass look ===
      const plaqueG = this.add.graphics().setDepth(800);
      const pw = 260, ph = 36, ppx = (GW - pw) / 2, ppy = GH - 42;
      // Shadow
      plaqueG.fillStyle(0x000000, 0.2); plaqueG.fillRoundedRect(ppx + 3, ppy + 3, pw, ph, 6);
      // Brass gradient
      plaqueG.fillGradientStyle(0xd8b878, 0xd8b878, 0xb89058, 0xb89058);
      plaqueG.fillRoundedRect(ppx, ppy, pw, ph, 6);
      // Inner bevel
      plaqueG.fillGradientStyle(0xe0c888, 0xe0c888, 0xc8a468, 0xc8a468);
      plaqueG.fillRoundedRect(ppx + 3, ppy + 3, pw - 6, ph - 6, 4);
      // Border line
      plaqueG.lineStyle(1.5, 0x907030); plaqueG.strokeRoundedRect(ppx, ppy, pw, ph, 6);
      plaqueG.lineStyle(0.5, 0xc0a050, 0.5); plaqueG.strokeRoundedRect(ppx + 4, ppy + 4, pw - 8, ph - 8, 3);
      // Decorative dots
      for (const dx of [ppx + 14, ppx + pw - 14]) {
        plaqueG.fillStyle(0xf0d080); plaqueG.fillCircle(dx, ppy + ph / 2, 3);
        plaqueG.fillStyle(0xd4b060); plaqueG.fillCircle(dx, ppy + ph / 2, 2);
      }
      this.plaqueName = this.add.text(GW / 2, ppy + ph / 2, "", {
        fontFamily: "'Courier New', monospace", fontSize: "16px", color: "#3a2a1a", fontStyle: "bold",
        stroke: "#d4b878", strokeThickness: 1,
      }).setOrigin(0.5).setDepth(801);

      if (this.roomData) this.loadRoom(this.roomData);
    }

    addRealWindow(x: number, y: number) {
      // Window frame from windows.png
      this.add.image(x + 32, y + 32, "windows_sheet")
        .setCrop(288, 192, 64, 64)
        .setDisplaySize(80, 80)
        .setDepth(2);

      // Window sill
      const sill = this.add.graphics().setDepth(2.1);
      sill.fillStyle(0xd0c8b8); sill.fillRect(x + 2, y + 72, 76, 5);
      sill.fillStyle(0xe0d8c8); sill.fillRect(x + 4, y + 72, 72, 2);

      // Animated clouds behind window — layered for depth
      for (let i = 0; i < 4; i++) {
        const cloud = this.add.graphics().setDepth(0.9);
        const size = 2 + i * 0.8;
        const alpha = 0.25 + (3 - i) * 0.08;
        cloud.fillStyle(0xffffff, alpha);
        cloud.fillCircle(0, 0, size);
        cloud.fillCircle(size * 1.3, -size * 0.3, size * 0.7);
        cloud.fillCircle(-size * 0.8, size * 0.2, size * 0.6);
        cloud.setPosition(x + (i % 2) * 30, y + 25 + i * 10);
        this.tweens.add({
          targets: cloud, x: x + 70,
          duration: 8000 + i * 3000, repeat: -1, delay: i * 2000,
          onRepeat: () => { cloud.x = x - 5; cloud.y = y + 25 + i * 10 + (Math.random() - 0.5) * 6; },
        });
      }

      // Subtle light shimmer on window glass
      const shimmer = this.add.graphics().setDepth(2.05);
      shimmer.fillStyle(0xffffff, 0.06); shimmer.fillRect(x + 10, y + 8, 8, 56);
      this.tweens.add({ targets: shimmer, alpha: { from: 0.06, to: 0.12 }, duration: 3000, yoyo: true, repeat: -1, ease: "Sine.easeInOut" });
    }

    addFurnitureFromSheet(x: number, y: number, sheet: string, sx: number, sy: number, sw: number, sh: number, scale: number, depth: number) {
      const img = this.add.image(x, y, sheet)
        .setCrop(sx, sy, sw, sh)
        .setDisplaySize(sw * scale, sh * scale)
        .setDepth(depth);
      return img;
    }

    addItemEffect(item: FurnItem, d: number) {
      const FX: Record<string, (scene: any, item: FurnItem, d: number) => void> = {
        coffee: (sc, it, dp) => {
          if (!sc.textures.exists("steam")) return;
          sc.add.particles(it.x, it.y - 20, "steam", {
            speed: { min: 3, max: 10 }, angle: { min: 250, max: 290 },
            scale: { start: 1, end: 0.2 }, alpha: { start: 0.35, end: 0 },
            lifespan: 1500, frequency: 500, quantity: 1,
          }).setDepth(dp + 2);
        },
        server: (sc, it, dp) => {
          const led = sc.add.graphics().setDepth(dp + 2);
          led.fillStyle(0x10b981); led.fillCircle(it.x - 5, it.y - 8, 2);
          sc.tweens.add({ targets: led, alpha: { from: 1, to: 0.2 }, duration: 800 + Math.random() * 400, yoyo: true, repeat: -1 });
        },
        fountain: (sc, it, dp) => {
          sc.add.particles(it.x, it.y - 25, "steam", {
            speed: { min: 5, max: 15 }, angle: { min: 240, max: 300 },
            scale: { start: 1.2, end: 0.3 }, alpha: { start: 0.4, end: 0 },
            lifespan: 1200, frequency: 300, quantity: 2, tint: 0x7ec8e3,
          }).setDepth(dp + 2);
        },
        arcade_machine: (sc, it, dp) => {
          const glow = sc.add.graphics().setDepth(dp + 1);
          glow.fillStyle(0x00ff88, 0.15); glow.fillEllipse(it.x, it.y - 8, 20, 12);
          sc.tweens.add({ targets: glow, alpha: { from: 0.15, to: 0.05 }, duration: 600 + Math.random() * 400, yoyo: true, repeat: -1 });
        },
        treadmill: (sc, it, dp) => {
          const belt = sc.add.graphics().setDepth(dp + 1);
          belt.fillStyle(0x00ff00, 0.2); belt.fillCircle(it.x - 3, it.y - 18, 1.5);
          sc.tweens.add({ targets: belt, alpha: { from: 0.3, to: 0.05 }, duration: 500, yoyo: true, repeat: -1 });
        },
        garden_lamp: (sc, it, dp) => {
          const g = sc.add.graphics().setDepth(dp - 1);
          g.fillStyle(0xffdd44, 0.08); g.fillEllipse(it.x, it.y - 10, 30, 40);
        },
        fish_tank: (sc, it, dp) => {
          const shimmer = sc.add.graphics().setDepth(dp + 1);
          shimmer.fillStyle(0x3498db, 0.1); shimmer.fillEllipse(it.x, it.y - 2, 25, 15);
          sc.tweens.add({ targets: shimmer, alpha: { from: 0.12, to: 0.03 }, scaleX: { from: 1, to: 1.1 }, duration: 1500, yoyo: true, repeat: -1, ease: "Sine.easeInOut" });
        },
        neon_sign: (sc, it, dp) => {
          const g = sc.add.graphics().setDepth(dp + 1);
          g.fillStyle(0xff6b9d, 0.08); g.fillEllipse(it.x, it.y, 35, 15);
          sc.tweens.add({ targets: g, alpha: { from: 0.1, to: 0.02 }, duration: 2000, yoyo: true, repeat: -1 });
        },
        desk_lamp: (sc, it, dp) => {
          const g = sc.add.graphics().setDepth(dp - 1);
          g.fillStyle(0xffdd88, 0.06); g.fillEllipse(it.x, it.y + 8, 20, 25);
        },
        speaker: (sc, it, dp) => {
          const p = sc.add.graphics().setDepth(dp + 1);
          p.fillStyle(0xffffff, 0.03); p.fillEllipse(it.x, it.y - 4, 12, 12);
          sc.tweens.add({ targets: p, scaleX: { from: 1, to: 1.4 }, scaleY: { from: 1, to: 1.4 }, alpha: { from: 0.05, to: 0 }, duration: 800, repeat: -1 });
        },
      };
      const fn = FX[item.key];
      if (fn) fn(this, item, d);
    }

    drawGoalBoard() {
      if (!this.roomData || this.roomData.goals.length === 0) return;
      const goals = this.roomData.goals;
      const bx = 770, by = 400;
      const bw = 160, bh = 34 + Math.min(goals.length, 4) * 34;
      const g = this.add.graphics().setDepth(400);
      // Shadow
      g.fillStyle(0x000000, 0.15); g.fillRoundedRect(bx + 3, by + 3, bw, bh, 8);
      // Card
      g.fillStyle(0x1e293b, 0.97); g.fillRoundedRect(bx, by, bw, bh, 7);
      // Header bar
      g.fillStyle(0x2a3a50, 0.6); g.fillRoundedRect(bx, by, bw, 24, { tl: 7, tr: 7, bl: 0, br: 0 });
      g.lineStyle(1.2, 0x3a5170, 0.6); g.strokeRoundedRect(bx, by, bw, bh, 7);
      this.add.text(bx + 10, by + 6, "\u25A0 GOALS", {
        fontFamily: "monospace", fontSize: "13px", color: "#8899bb", fontStyle: "bold",
      }).setDepth(401);
      for (let i = 0; i < Math.min(goals.length, 4); i++) {
        const gy = by + 30 + i * 34;
        const label = goals[i].label.length > 16 ? goals[i].label.slice(0, 16) + ".." : goals[i].label;
        this.add.text(bx + 10, gy, label, { fontFamily: "monospace", fontSize: "12px", color: "#e7e5e4" }).setDepth(401);
        // Track background
        g.fillStyle(0x0f172a); g.fillRoundedRect(bx + 10, gy + 15, bw - 20, 8, 4);
        const p = goals[i].progress;
        const barCol = p >= 70 ? 0x10b981 : p >= 40 ? 0xf59e0b : 0xef4444;
        const barW = Math.max(4, (bw - 20) * p / 100);
        g.fillStyle(barCol, 0.85); g.fillRoundedRect(bx + 10, gy + 15, barW, 8, 4);
        // Shine on bar
        g.fillStyle(0xffffff, 0.15); g.fillRoundedRect(bx + 10, gy + 15, barW, 3, { tl: 4, tr: 4, bl: 0, br: 0 });
        this.add.text(bx + bw - 12, gy + 16, `${p}%`, { fontFamily: "monospace", fontSize: "11px", color: "#a8a29e", fontStyle: "bold" }).setOrigin(1, 0).setDepth(401);
      }
    }

    drawTaskStickies() {
      if (!this.roomData || this.roomData.tasks.length === 0) return;
      const colors: Record<string, number> = {
        completed: 0x10b981, done: 0x10b981, in_progress: 0x3b82f6, running: 0x3b82f6,
        pending: 0xf59e0b, scheduled: 0xf59e0b, failed: 0xef4444, error: 0xef4444, proposed: 0x8b5cf6,
      };
      const sx = 540, sy = 105;
      const container = this.add.container(0, 0).setDepth(3);
      this.roomData.tasks.slice(0, 5).forEach((t, i) => {
        const c = colors[t.status.toLowerCase()] || 0x94a3b8;
        const g = this.add.graphics();
        // Sticky note effect with fold
        g.fillStyle(c, 0.12); g.fillRoundedRect(sx + i * 38, sy, 34, 32, 4);
        g.fillStyle(c, 0.08); g.fillTriangle(sx + i * 38 + 28, sy, sx + i * 38 + 34, sy, sx + i * 38 + 34, sy + 6);
        g.lineStyle(0.8, c, 0.35); g.strokeRoundedRect(sx + i * 38, sy, 34, 32, 4);
        container.add(g);
        const numTxt = this.add.text(sx + i * 38 + 17, sy + 9, String(t.count), {
          fontFamily: "monospace", fontSize: "15px", fontStyle: "bold",
          color: "#" + c.toString(16).padStart(6, "0"),
        }).setOrigin(0.5);
        container.add(numTxt);
        const labelTxt = this.add.text(sx + i * 38 + 17, sy + 24, t.status.slice(0, 6), {
          fontFamily: "monospace", fontSize: "10px",
          color: "#" + c.toString(16).padStart(6, "0"),
        }).setOrigin(0.5);
        container.add(labelTxt);
      });
    }

    // ── Load Room ─────────────────────────────────────────────────────
    loadRoom(room: RoomData) {
      this.roomData = room;
      if (this.plaqueName) this.plaqueName.setText(room.name);

      this.agentSprites.forEach((s: any) => s.container.destroy());
      this.agentSprites.clear();
      this.children.list.filter((c: any) => c.getData?.("isDesk")).forEach((c: any) => c.destroy());

      this.drawGoalBoard();
      this.drawTaskStickies();

      let deskIdx = 0;
      // Desk accessories alternate per desk for variety
      const deskAccessories = [
        { cup: true, papers: false, plant: true, phone: false },
        { cup: false, papers: true, plant: false, phone: true },
        { cup: true, papers: true, plant: false, phone: false },
        { cup: false, papers: false, plant: true, phone: true },
        { cup: true, papers: false, plant: false, phone: true },
        { cup: false, papers: true, plant: true, phone: false },
      ];

      room.agents.forEach((agent, i) => {
        if (agent.status === "working") {
          const slot = this.layout.deskSlots[deskIdx % this.layout.deskSlots.length];
          const acc = deskAccessories[deskIdx % deskAccessories.length];
          // Place desk from LPC furniture sheet
          const deskImg = this.addFurnitureFromSheet(slot.x, slot.y, "furniture", 128, 32, 64, 64, 1.5, slot.y + 5);
          deskImg.setData("isDesk", true);
          // Chair behind desk
          this.add.image(slot.x, slot.y + 26, "chair").setScale(1.3).setDepth(slot.y + 4).setData("isDesk", true);
          // Monitor on desk
          const mon = this.add.image(slot.x, slot.y - 22, "monitor").setScale(1.4).setDepth(slot.y + 6);
          mon.setData("isDesk", true);
          // Monitor glow on floor
          const monGlow = this.add.graphics().setDepth(slot.y + 3);
          monGlow.fillStyle(0x60a0f0, 0.04); monGlow.fillEllipse(slot.x, slot.y + 8, 60, 16);
          monGlow.setData("isDesk", true);
          // Keyboard
          this.add.image(slot.x, slot.y - 4, "keyboard").setScale(1.5).setDepth(slot.y + 6).setData("isDesk", true);
          // Desk lamp
          this.add.image(slot.x + 34, slot.y - 16, "lamp").setScale(1.2).setDepth(slot.y + 6).setData("isDesk", true);
          // Lamp glow on desk
          const lampGlow = this.add.graphics().setDepth(slot.y + 5.5);
          lampGlow.fillStyle(0xfff0a0, 0.04); lampGlow.fillEllipse(slot.x + 34, slot.y - 4, 30, 12);
          lampGlow.setData("isDesk", true);
          // Accessories
          if (acc.cup) this.add.image(slot.x - 28, slot.y - 14, "coffee_cup").setScale(1.3).setDepth(slot.y + 6).setData("isDesk", true);
          if (acc.papers) this.add.image(slot.x + 24, slot.y - 6, "paper_stack").setScale(1.2).setDepth(slot.y + 6).setData("isDesk", true);
          if (acc.plant) this.add.image(slot.x - 30, slot.y - 20, "small_plant").setScale(1.2).setDepth(slot.y + 6).setData("isDesk", true);
          if (acc.phone) this.add.image(slot.x + 30, slot.y - 8, "phone").setScale(1.3).setDepth(slot.y + 6).setData("isDesk", true);
          // Trash can beside desk
          if (deskIdx % 2 === 0) this.add.image(slot.x + 42, slot.y + 16, "trash_can").setScale(1.3).setDepth(slot.y + 7).setData("isDesk", true);
          this.spawnAgent(agent, i, slot.x, slot.y - 10, true);
          deskIdx++;
        } else {
          const idle = this.layout.idleSlots[i % this.layout.idleSlots.length];
          this.spawnAgent(agent, i, idle.x, idle.y, false);
        }
      });

      // Empty desks with chairs and off monitors — still look lived-in
      for (let d = deskIdx; d < Math.max(deskIdx + 1, 2); d++) {
        const slot = this.layout.deskSlots[d % this.layout.deskSlots.length];
        const acc = deskAccessories[(d + 2) % deskAccessories.length];
        this.addFurnitureFromSheet(slot.x, slot.y, "furniture", 128, 32, 64, 64, 1.5, slot.y + 5).setData("isDesk", true);
        this.add.image(slot.x, slot.y + 26, "chair").setScale(1.3).setDepth(slot.y + 4).setData("isDesk", true);
        this.add.image(slot.x, slot.y - 22, "monitor_off").setScale(1.4).setDepth(slot.y + 6).setData("isDesk", true);
        this.add.image(slot.x, slot.y - 4, "keyboard").setScale(1.5).setDepth(slot.y + 6).setData("isDesk", true);
        this.add.image(slot.x + 34, slot.y - 16, "lamp").setScale(1.2).setDepth(slot.y + 6).setData("isDesk", true);
        if (acc.papers) this.add.image(slot.x + 24, slot.y - 6, "paper_stack").setScale(1.2).setDepth(slot.y + 6).setData("isDesk", true);
        if (acc.plant) this.add.image(slot.x - 30, slot.y - 20, "small_plant").setScale(1.2).setDepth(slot.y + 6).setData("isDesk", true);
      }
    }

    spawnAgent(agent: AgentData, index: number, tx: number, ty: number, sitting: boolean) {
      const startX = GW / 2, startY = GH + 20;
      const container = this.add.container(startX, startY).setDepth(ty + 20);
      container.setData("target", { x: tx, y: ty });

      // Shadow
      container.add(this.add.image(0, sitting ? 16 : 20, "shadow").setScale(1.6));

      // Character sprite — REAL pixel art with animation!
      const charKey = CHAR_KEYS[agent.charIdx % CHAR_KEYS.length];
      const body = this.add.sprite(0, 0, charKey, 0).setScale(3);
      container.add(body);
      body.setFrame(0);

      // Status dot with glow
      container.add(this.add.image(18, -24, agent.status === "working" ? "dot_green" : "dot_yellow").setScale(1.3));

      // Name tag — cleaner look
      const nameTag = this.add.text(0, sitting ? 26 : 30, agent.name, {
        fontFamily: "'Courier New', monospace", fontSize: "13px",
        color: "#2a1a0a", stroke: "#ffffff", strokeThickness: 3, fontStyle: "bold",
      }).setOrigin(0.5);
      container.add(nameTag);

      // Click for info
      body.setInteractive({ useHandCursor: true });
      body.on("pointerdown", () => this.showInfoCard(agent, container));

      // Subtle typing motion for working agents
      if (sitting) {
        this.tweens.add({ targets: body, y: { from: 0, to: -1 }, duration: 800, yoyo: true, repeat: -1, ease: "Sine.easeInOut" });
      }

      // Bubble timer
      const delay = 4000 + Math.random() * 6000;
      this.time.addEvent({ delay, loop: true, callback: () => this.showAgentBubble(agent.id) });
      this.time.delayedCall(2000 + Math.random() * 3000, () => this.showAgentBubble(agent.id));

      this.agentSprites.set(agent.id, {
        container, body, bubble: null, status: agent.status, charIdx: agent.charIdx,
        walking: false, doingAction: null, deskSlot: sitting ? { x: tx, y: ty + 10 } : null,
      });
    }

    // ── Cat Actions ───────────────────────────────────────────────
    performCatAction() {
      if (!this.catContainer) return;
      const action = CAT_ACTION_DEFS[Math.floor(Math.random() * CAT_ACTION_DEFS.length)];
      // Skip actions whose spot doesn't exist in this layout
      if (action.spotKey && !this.layout.actionSpots[action.spotKey]) return;
      this.catAction = action.name;
      this.catSpeed = action.speed;

      if (action.bubble) this.showCatActionBubble(action.bubble);
      if (this.catZzz) this.catZzz.setAlpha(action.name === "sleep" ? 0.5 : 0);
      if (action.name !== "sleep" && this.catImg) {
        this.tweens.add({ targets: this.catImg, scaleX: 1.8, scaleY: 1.8, duration: 300 });
      }

      switch (action.name) {
        case "sleep":
          this.catTarget = null;
          if (this.catImg) this.tweens.add({ targets: this.catImg, scaleX: 1.6, scaleY: 1.5, duration: 800, ease: "Sine.easeInOut" });
          break;

        case "wander":
        case "trot": {
          const cwp = this.layout.catWanderPoints;
          const pt = cwp[Math.floor(Math.random() * cwp.length)];
          this.catTarget = pt;
          this.catContainer.setDepth(pt.y + 10);
          break;
        }

        case "desk": {
          const slots = this.layout.deskSlots;
          const slot = slots[Math.floor(Math.random() * slots.length)];
          this.catTarget = { x: slot.x + 20, y: slot.y - 30 };
          this.catContainer.setDepth(slot.y + 50);
          break;
        }

        case "sofa":
        case "plant":
        case "window": {
          const spot = action.spotKey ? this.layout.actionSpots[action.spotKey] : null;
          if (spot) {
            this.catTarget = spot;
            this.catContainer.setDepth(spot.y + 10);
          }
          break;
        }

        case "follow": {
          const agents = Array.from(this.agentSprites.values());
          if (agents.length > 0) {
            const target = agents[Math.floor(Math.random() * agents.length)];
            const c = target.container;
            this.catTarget = { x: c.x + 30, y: c.y + 10 };
            this.catContainer.setDepth(c.y + 30);
          }
          break;
        }

        case "stare":
          this.catTarget = null;
          if (this.catImg) this.tweens.add({ targets: this.catImg, scaleX: 1.9, scaleY: 1.9, duration: 300, yoyo: true, hold: 1500, ease: "Sine.easeInOut" });
          break;

        case "groom":
          this.catTarget = null;
          if (this.catImg) this.tweens.add({ targets: this.catImg, y: { from: 0, to: 1 }, duration: 300, yoyo: true, repeat: 3 });
          break;

        case "stretch":
          this.catTarget = null;
          if (this.catImg) this.tweens.add({ targets: this.catImg, scaleX: 2.0, scaleY: 1.6, duration: 400, yoyo: true, ease: "Sine.easeInOut" });
          break;
      }
    }

    showCatActionBubble(text: string) {
      if (!this.catContainer) return;
      if (this.catBubble) { this.catBubble.destroy(); this.catBubble = null; }
      const bubble = this.makeBubble(text, 0xfffbeb, "#8b6914", 0xd4a574);
      bubble.setPosition(22, -26);
      this.catContainer.add(bubble);
      this.catBubble = bubble;
      bubble.setScale(0);
      this.tweens.add({ targets: bubble, scaleX: 1, scaleY: 1, duration: 180, ease: "Back.easeOut" });
      this.time.delayedCall(4000, () => {
        if (this.catBubble === bubble) {
          this.tweens.add({ targets: bubble, alpha: 0, duration: 120, onComplete: () => { bubble.destroy(); this.catBubble = null; } });
        }
      });
    }

    // ── Idle Agent Actions ──────────────────────────────────────────
    performIdleAgentActions() {
      this.agentSprites.forEach((s: any) => {
        if (s.status !== "idle") return;
        if (s.doingAction) return;
        if (Math.random() > 0.6) return;

        const action = IDLE_ACTION_DEFS[Math.floor(Math.random() * IDLE_ACTION_DEFS.length)];
        let spot: Pt | undefined;
        if (!action.spotKey || action.name === "wander") {
          const wp = this.layout.wanderPoints;
          spot = wp[Math.floor(Math.random() * wp.length)];
        } else {
          spot = this.layout.actionSpots[action.spotKey];
        }
        if (!spot) return; // spot not available in this layout
        s.doingAction = action.name;
        s.container.setData("target", { x: spot.x, y: spot.y });
        s.container.setDepth(spot.y + 20);

        this.showActionBubble(s, action.bubble);

        this.time.delayedCall(action.duration, () => {
          if (s.doingAction === action.name) {
            s.doingAction = null;
            const isl = this.layout.idleSlots;
            const idleSpot = isl[Math.floor(Math.random() * isl.length)];
            s.container.setData("target", { x: idleSpot.x, y: idleSpot.y });
            s.container.setDepth(idleSpot.y + 20);
          }
        });
      });
    }

    // ── Working Agent Actions (occasional desk break) ───────────────
    performWorkingAgentActions() {
      this.agentSprites.forEach((s: any) => {
        if (s.status !== "working") return;
        if (s.doingAction) return;
        if (Math.random() > 0.25) return;

        const action = WORKING_ACTION_DEFS[Math.floor(Math.random() * WORKING_ACTION_DEFS.length)];
        const spot = this.layout.actionSpots[action.spotKey];
        if (!spot) return; // spot not available in this layout
        s.doingAction = action.name;
        s.container.setData("target", { x: spot.x, y: spot.y });
        s.container.setDepth(spot.y + 20);

        this.showActionBubble(s, action.bubble);

        this.time.delayedCall(action.duration, () => {
          if (s.doingAction === action.name) {
            s.doingAction = null;
            const deskSlot = s.deskSlot;
            if (deskSlot) {
              s.container.setData("target", { x: deskSlot.x, y: deskSlot.y - 10 });
              s.container.setDepth(deskSlot.y + 20);
            }
          }
        });
      });
    }

    showActionBubble(s: any, text: string) {
      if (s.bubble) { s.bubble.destroy(); s.bubble = null; }
      const bubble = this.makeBubble(text, 0xffffff, "#292524");
      bubble.setPosition(0, -34);
      s.container.add(bubble);
      s.bubble = bubble;
      bubble.setScale(0);
      this.tweens.add({ targets: bubble, scaleX: 1, scaleY: 1, duration: 180, ease: "Back.easeOut" });
      this.time.delayedCall(BUBBLE_DUR, () => {
        if (s.bubble === bubble) {
          this.tweens.add({ targets: bubble, alpha: 0, scaleY: 0, duration: 120, onComplete: () => { bubble.destroy(); if (s.bubble === bubble) s.bubble = null; } });
        }
      });
    }

    // ── Agent emote animations (no rotation — pixel characters stay upright) ──
    agentEmote(s: any, type: string) {
      if (!s || !s.body) return;
      const EMOTE_TWEENS: Record<string, object> = {
        nod:       { y: { from: 0, to: 2 }, duration: 200, yoyo: true, repeat: 1 },
        sip:       { y: { from: 0, to: -1 }, duration: 300, yoyo: true, hold: 400, repeat: 1 },
        sit:       { y: { from: 0, to: 4 }, duration: 500, ease: "Sine.easeOut" },
        stretch:   { scaleY: { from: 3, to: 3.3 }, duration: 500, yoyo: true, ease: "Sine.easeInOut" },
        type:      { y: { from: 0, to: -1 }, duration: 100, yoyo: true, repeat: 4 },
        wave:      { x: { from: 0, to: 2 }, duration: 150, yoyo: true, repeat: 3 },
        jump:      { y: { from: 0, to: -5 }, duration: 200, yoyo: true, ease: "Quad.easeOut" },
        lean:      { y: { from: 0, to: 3 }, scaleY: { from: 3, to: 2.8 }, duration: 600, ease: "Sine.easeInOut" },
        meditate:  { scaleY: { from: 3, to: 3.1 }, duration: 1200, yoyo: true, repeat: 2, ease: "Sine.easeInOut" },
        point:     { x: { from: 0, to: 3 }, duration: 300, yoyo: true, hold: 500 },
        celebrate: { y: { from: 0, to: -4 }, duration: 150, yoyo: true, repeat: 2, ease: "Quad.easeOut" },
      };
      const cfg = EMOTE_TWEENS[type];
      if (cfg) this.tweens.add({ targets: s.body, ...cfg });
    }

    showInfoCard(agent: AgentData, container: any) {
      if (this.infoCard) { this.infoCard.destroy(); this.infoCard = null; }
      const card = this.add.container(container.x, container.y - 60).setDepth(2000);
      const bg = this.add.graphics();
      // Shadow
      bg.fillStyle(0x000000, 0.25); bg.fillRoundedRect(-82, -28, 166, 62, 10);
      // Card background
      bg.fillStyle(0x1e293b, 0.97); bg.fillRoundedRect(-84, -30, 168, 62, 9);
      // Subtle gradient top
      bg.fillStyle(0x2a3a50, 0.5); bg.fillRoundedRect(-84, -30, 168, 20, { tl: 9, tr: 9, bl: 0, br: 0 });
      // Border
      bg.lineStyle(1.5, 0x3a5170, 0.8); bg.strokeRoundedRect(-84, -30, 168, 62, 9);
      // Status indicator bar
      const stBarCol = agent.status === "working" ? 0x10b981 : 0xf59e0b;
      bg.fillStyle(stBarCol, 0.8); bg.fillRoundedRect(-84, 28, 168, 4, { tl: 0, tr: 0, bl: 9, br: 9 });
      card.add(bg);
      card.add(this.add.text(0, -18, agent.name, {
        fontFamily: "'Courier New', monospace", fontSize: "16px", color: "#f0e8d8", fontStyle: "bold",
      }).setOrigin(0.5));
      const stCol = agent.status === "working" ? "#4f9c84" : "#cf9b44";
      const stIcon = agent.status === "working" ? "\u25CF " : "\u25CB ";
      card.add(this.add.text(0, 4, stIcon + (agent.status === "working" ? "Working" : "Idle"), {
        fontFamily: "'Courier New', monospace", fontSize: "13px", color: stCol, fontStyle: "bold",
      }).setOrigin(0.5));
      card.setScale(0);
      this.tweens.add({ targets: card, scaleX: 1, scaleY: 1, duration: 200, ease: "Back.easeOut" });
      this.infoCard = card;
      this.time.delayedCall(3000, () => {
        if (this.infoCard === card) {
          this.tweens.add({ targets: card, alpha: 0, scaleY: 0, duration: 150, onComplete: () => { card.destroy(); this.infoCard = null; } });
        }
      });
    }

    blinkAgents() {
      this.agentSprites.forEach((s: any) => {
        this.tweens.add({ targets: s.body, scaleY: { from: 3, to: 2.6 }, duration: 60, yoyo: true, ease: "Quad.easeIn" });
      });
    }

    showAgentBubble(agentId: string) {
      const s = this.agentSprites.get(agentId);
      if (!s || s.doingAction) return;
      const bubbles = s.status === "working" ? WORK_BUBBLES : IDLE_BUBBLES;
      this.showActionBubble(s, bubbles[Math.floor(Math.random() * bubbles.length)]);
      // Working agents occasionally do a desk emote
      if (s.status === "working" && Math.random() < 0.25) {
        this.agentEmote(s, Math.random() < 0.6 ? "type" : "stretch");
      }
    }

    showCatBubble() {
      const text = CAT_BUBBLES[Math.floor(Math.random() * CAT_BUBBLES.length)];
      this.showCatActionBubble(text);
    }

    makeBubble(text: string, bg: number, textCol: string, border?: number): any {
      const t = this.add.text(0, 0, text, {
        fontFamily: "'Courier New', monospace", fontSize: "13px", color: textCol, fontStyle: "bold",
      }).setOrigin(0.5);
      const pad = 10, w = t.width + pad * 2, h = t.height + pad;
      const gfx = this.add.graphics();
      // Shadow
      gfx.fillStyle(0x000000, 0.08); gfx.fillRoundedRect(-w / 2 + 2, -h / 2 + 2, w, h, 7);
      // Background
      gfx.fillStyle(bg, 0.95); gfx.fillRoundedRect(-w / 2, -h / 2, w, h, 7);
      // Subtle inner highlight
      gfx.fillStyle(0xffffff, 0.08); gfx.fillRoundedRect(-w / 2 + 1, -h / 2 + 1, w - 2, h / 2, { tl: 6, tr: 6, bl: 0, br: 0 });
      // Border
      gfx.lineStyle(1.2, border || 0xd8d0c0, 0.7); gfx.strokeRoundedRect(-w / 2, -h / 2, w, h, 7);
      // Tail
      gfx.fillStyle(bg, 0.95); gfx.fillTriangle(-4, h / 2, 4, h / 2, 0, h / 2 + 7);
      const c = this.add.container(0, 0, [gfx, t]).setDepth(500);
      return c;
    }
  }

  return OfficeScene;
}

// ══════════════════════════════════════════════════════════════════════
//  REACT WRAPPER
// ══════════════════════════════════════════════════════════════════════
function useRooms() {
  const { data: workspaces, isLoading: isLoadingWs } = useQuery({ queryKey: ["workspaces"], queryFn: () => api.workspaces.list() });
  const wsIds = useMemo(() => ((workspaces as any[]) || []).map((w: any) => w.id), [workspaces]);
  const hasWs = wsIds.length > 0;
  const { data: allDash, isLoading: isLoadingDash } = useQuery({
    queryKey: ["mo-d", wsIds], enabled: hasWs,
    queryFn: async () => { const r: Record<string, any> = {}; await Promise.all(wsIds.map(async (id: string) => { try { r[id] = await api.workspaces.dashboard(id); } catch {} })); return r; },
  });
  const { data: allModels, isLoading: isLoadingModels } = useQuery({
    queryKey: ["mo-m", wsIds], enabled: hasWs,
    queryFn: async () => { const r: Record<string, any> = {}; await Promise.all(wsIds.map(async (id: string) => { try { const raw = await api.workspaces.operatingModel(id); r[id] = (raw as any)?.operating_model ?? raw; } catch {} })); return r; },
  });
  const { data: allMap, isLoading: isLoadingMap } = useQuery({
    queryKey: ["mo-a", wsIds], enabled: hasWs,
    queryFn: async () => { const r: Record<string, any[]> = {}; await Promise.all(wsIds.map(async (id: string) => { try { r[id] = (await api.workspaces.agents.list(id)) as any[]; } catch {} })); return r; },
  });
  const { data: entityAgents, isLoading: isLoadingEa } = useQuery({ queryKey: ["mo-ea"], enabled: hasWs, queryFn: () => api.agents.list() });
  const isLoading = isLoadingWs || (hasWs && (isLoadingDash || isLoadingModels || isLoadingMap || isLoadingEa));

  const rooms: RoomData[] = useMemo(() => {
    if (!workspaces) return [];
    const agentById = new Map<string, any>();
    for (const a of (entityAgents as any[]) || []) agentById.set(a.id, a);
    return ((workspaces as any[]) || []).map((ws: any, wsIdx: number) => {
      const model = allModels?.[ws.id];
      const mappings = allMap?.[ws.id] || [];
      const dash = allDash?.[ws.id];
      const services = (model?.services as any[]) || [];
      const mappingsBySvc = new Map<string, any>();
      for (const m of mappings) if (m.service_key) mappingsBySvc.set(m.service_key, m);
      let deskIdx = 0;
      const agents: AgentData[] = services.map((svc: any, i: number) => {
        const key = svc.service_key || svc.key || "";
        const m = mappingsBySvc.get(key);
        const agent = m ? agentById.get(m.agent_id) : null;
        if (!agent) return null;
        const h = hashStr(agent.id || "");
        const status: "working" | "idle" = h % 3 === 0 ? "idle" : "working";
        let tx: number, ty: number;
        const wsLayout = OFFICE_LAYOUTS[wsIdx % OFFICE_LAYOUTS.length];
        if (status === "working") { const slot = wsLayout.deskSlots[deskIdx % wsLayout.deskSlots.length]; tx = slot.x; ty = slot.y - 10; deskIdx++; }
        else { const idle = wsLayout.idleSlots[i % wsLayout.idleSlots.length]; tx = idle.x; ty = idle.y; }
        return { id: agent.id, name: agent.name || "Agent", charIdx: (wsIdx * 3 + i + h) % CHAR_KEYS.length, status, tx, ty } as AgentData;
      }).filter(Boolean) as AgentData[];
      const goals = ((model?.goals as any[]) || []).map((g: any) => ({
        label: g.title || g.name || humanize(g.goal_key || g.key || "Goal"),
        progress: g.progress != null ? g.progress : Math.floor(20 + Math.random() * 65),
      }));
      const tasks = Object.entries(dash?.tasks_by_status || {}).map(([status, count]) => ({ status, count: count as number }));
      return { workspaceId: ws.id, name: ws.name || "Unnamed", status: ws.status || "active", agents, goals, tasks, totalTasks: dash?.total_tasks ?? 0, totalDocs: dash?.total_documents ?? 0, themeIdx: wsIdx };
    });
  }, [workspaces, allDash, allModels, allMap, entityAgents]);
  return { rooms, isLoading };
}

const STYLE_ID = "manor-office-v7";
function injectCSS() {
  if (document.getElementById(STYLE_ID)) return;
  const s = document.createElement("style");
  s.id = STYLE_ID;
  s.textContent = `
.mo-root{width:100%;height:100%;position:relative;background:#12121e;overflow:hidden;user-select:none}
.mo-viewport{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;overflow:hidden;background:#1a1a2e}
.mo-viewport canvas{image-rendering:pixelated !important;display:block}
.mo-hud{display:flex;align-items:center;gap:20px;padding:8px 24px;position:absolute;bottom:0;left:0;right:0;z-index:10;background:linear-gradient(180deg,rgba(20,20,30,.75),rgba(16,16,26,.9));backdrop-filter:blur(6px);border-top:1px solid rgba(42,42,58,.5);font-size:12px;color:#6a6a8a;font-family:'Courier New',monospace}
.mo-hud b{color:#b0b0d0;font-weight:800}
.mo-hud span{letter-spacing:.3px}
.mo-live{display:flex;align-items:center;gap:6px}
.mo-live-dot{width:7px;height:7px;border-radius:50%;background:#4f9c84;box-shadow:0 0 6px #4f9c84,0 0 12px rgba(79,156,132,.3);animation:mo-p 2s ease infinite}
@keyframes mo-p{0%,100%{opacity:1}50%{opacity:.3}}
.mo-sep{width:1px;height:18px;background:linear-gradient(180deg,transparent,#3a3a5a,transparent)}
.mo-ws-btn{background:linear-gradient(180deg,#2a2a44,#222240);border:1px solid #3a3a5a;color:#aac;border-radius:6px;padding:5px 16px;font-size:12px;font-weight:700;cursor:pointer;font-family:inherit;transition:all .2s;letter-spacing:.3px}
.mo-ws-btn:hover{background:linear-gradient(180deg,#3a3a5a,#2a2a44);color:#dde;border-color:#4a4a6a;box-shadow:0 2px 8px rgba(0,0,0,.3)}
  `;
  document.head.appendChild(s);
}

export default function ManorOffice({ workspaceId: singleWsId }: { workspaceId?: string } = {}) {
  const navigate = useNavigate();
  const gameRef = useRef<any>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const [phaserReady, setPhaserReady] = useState(!!(window as any).Phaser);
  const [phaserError, setPhaserError] = useState(false);
  const [sceneInfo, setSceneInfo] = useState<{ scene: string; room?: RoomData; rooms?: RoomData[] }>({ scene: "town" });
  const { rooms: allRooms, isLoading } = useRooms();
  const rooms = useMemo(() => singleWsId ? allRooms.filter(r => r.workspaceId === singleWsId) : allRooms, [allRooms, singleWsId]);

  useEffect(() => { injectCSS(); }, []);
  useEffect(() => {
    if ((window as any).Phaser) { setPhaserReady(true); return; }
    loadPhaser().then(() => setPhaserReady(true)).catch((e) => { console.error("Phaser load failed:", e); setPhaserError(true); });
  }, []);

  // Create game once Phaser is ready (don't wait for rooms — town shows empty plots)
  useEffect(() => {
    if (!phaserReady || !containerRef.current || isLoading) return;
    const Phaser = (window as any).Phaser;
    if (!Phaser || !Phaser.Game) { setPhaserError(true); return; }

    try {
      const TownScene = createTownScene(Phaser);
      const OfficeScene = createOfficeScene(Phaser);

      // If singleWsId with rooms, skip town and go straight to office
      const directToOffice = singleWsId && rooms.length > 0;

      const townScene = new TownScene();
      townScene.rooms = rooms;
      const officeScene = new OfficeScene();
      if (directToOffice) {
        officeScene.roomData = rooms[0] ?? null;
        officeScene.allRooms = [];
      }

      const game = new Phaser.Game({
        type: Phaser.AUTO, width: GW, height: GH,
        parent: containerRef.current, backgroundColor: "#1a1a2e",
        render: { antialias: true, pixelArt: false, roundPixels: true },
        scale: { mode: Phaser.Scale.FIT, autoCenter: Phaser.Scale.CENTER_BOTH, width: GW, height: GH },
        scene: directToOffice ? [officeScene, townScene] : [townScene, officeScene],
      });
      gameRef.current = game;

      game.events.on("sceneInfo", (info: any) => setSceneInfo(info));

      return () => { game.events.off("sceneInfo"); game.destroy(true); gameRef.current = null; };
    } catch (e) {
      console.error("Phaser game creation failed:", e);
      setPhaserError(true);
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [phaserReady, isLoading]);

  const totalAgents = rooms.reduce((s, r) => s + r.agents.length, 0);
  const totalWorking = rooms.reduce((s, r) => s + r.agents.filter(a => a.status === "working").length, 0);
  const totalTasks = rooms.reduce((s, r) => s + r.totalTasks, 0);
  const currentRoom = sceneInfo.room;
  const isTown = sceneInfo.scene === "town";

  if (isLoading || !phaserReady) {
    return (
      <div className="mo-root" style={{ display: "flex", alignItems: "center", justifyContent: "center" }}>
        {phaserError ? (
          <span style={{ fontSize: 13, color: "#d65f59", fontFamily: "'Courier New', monospace" }}>{t("page.manor_office.failed_to_load_game_engine_please_refresh")}</span>
        ) : (
          <>
            <LoadingSpinner size={20} />
            <span style={{ fontSize: 13, color: "#6a6a8a", marginTop: 8, fontFamily: "'Courier New', monospace" }}>
              {!phaserReady ? t("page.manor_office.loading_game_engine") : t("page.manor_office.opening_the_manor")}
            </span>
          </>
        )}
      </div>
    );
  }

  return (
    <div className="mo-root">
      <div className="mo-viewport" ref={containerRef} />
      <div className="mo-hud">
        <span className="mo-live"><span className="mo-live-dot" /><span style={{ fontWeight: 700, color: "#4f9c84", letterSpacing: 1, fontSize: 11 }}>{t("page.manor_office.live")}</span></span>
        <span className="mo-sep" />
        {isTown ? (
          <>
            <span>{t("nav.workspaces")} <b>{rooms.length}</b></span>
            <span className="mo-sep" />
            <span>{t("nav.agents")} <b>{totalAgents}</b></span>
            <span>{t("page.workspaces.filter_active")} <b style={{ color: "#4f9c84" }}>{totalWorking}</b></span>
            <span className="mo-sep" />
            <span>{t("nav.tasks")} <b style={{ color: "#9079c2" }}>{totalTasks}</b></span>
            <span style={{ flex: 1 }} />
            <span style={{ color: "#6a6a8a", fontSize: 11, fontStyle: "italic" }}>{t("page.manor_office.click_a_building_to_enter")}</span>
          </>
        ) : (
          <>
            {currentRoom && <span style={{ color: "#f0d890", fontWeight: 700 }}>{currentRoom.name}</span>}
            <span className="mo-sep" />
            <span>{t("nav.agents")} <b>{currentRoom?.agents.length ?? 0}</b></span>
            <span>{t("page.workspaces.filter_active")} <b style={{ color: "#4f9c84" }}>{currentRoom?.agents.filter(a => a.status === "working").length ?? 0}</b></span>
            <span className="mo-sep" />
            <span>{t("nav.tasks")} <b style={{ color: "#9079c2" }}>{currentRoom?.totalTasks ?? 0}</b></span>
            {currentRoom && <span>{t("page.manor_office.docs")} <b>{currentRoom.totalDocs}</b></span>}
            <span style={{ flex: 1 }} />
            {!singleWsId && currentRoom && <button className="mo-ws-btn" onClick={() => navigate(`/workspaces/${currentRoom.workspaceId}`)}>{t("page.manor_office.open_workspace")}</button>}
          </>
        )}
      </div>
    </div>
  );
}
