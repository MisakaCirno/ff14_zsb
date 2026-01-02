import type { IRect } from 'konva/lib/types'
import type { IconType, StrategyObject } from 'xiv-strat-board'

export const SIZE32 = 32
export const SIZE48 = 48
export const SIZE64 = 64
export const SIZE128 = 128
export const SIZE256 = 256
export const SIZE512 = 512

type IconMap = Partial<Record<IconType, number>>

export const tab1: IconMap = {
  //role
  tank: SIZE32,
  tank_1: SIZE32,
  tank_2: SIZE32,
  healer: SIZE32,
  healer_1: SIZE32,
  healer_2: SIZE32,
  pure_healer: SIZE32,
  barrier_healer: SIZE32,
  dps: SIZE32,
  dps_1: SIZE32,
  dps_2: SIZE32,
  dps_3: SIZE32,
  dps_4: SIZE32,
  melee_dps: SIZE32,
  ranged_dps: SIZE32,
  physical_ranged_dps: SIZE32,
  magical_ranged_dps: SIZE32,

  //job
  paladin: SIZE32,
  warrior: SIZE32,
  dark_knight: SIZE32,
  gunbreaker: SIZE32,

  white_mage: SIZE32,
  scholar: SIZE32,
  astrologian: SIZE32,
  sage: SIZE32,

  monk: SIZE32,
  dragoon: SIZE32,
  ninja: SIZE32,
  samurai: SIZE32,
  reaper: SIZE32,
  viper: SIZE32,

  bard: SIZE32,
  machinist: SIZE32,
  dancer: SIZE32,

  black_mage: SIZE32,
  summoner: SIZE32,
  red_mage: SIZE32,
  pictomancer: SIZE32,
  blue_mage: SIZE32,
  gladiator: SIZE32,
  marauder: SIZE32,
  conjurer: SIZE32,

  pugilist: SIZE32,
  lancer: SIZE32,
  rogue: SIZE32,
  archer: SIZE32,
  thaumaturge: SIZE32,
  arcanist: SIZE32,
}

//tab2 (64x64 in game)
export const tab2: IconMap = {
  gaze: SIZE128,
  stack: SIZE128,
  line_stack: SIZE128,
  proximity: SIZE256,
  stack_multi: SIZE128,
  proximity_player: SIZE128,
  tankbuster: SIZE64,
  radial_knockback: SIZE256,
  linear_knockback: SIZE256,
  tower: SIZE64,
  targeting: SIZE64,
  moving_circle_aoe: SIZE128,
  '1person_aoe': SIZE64,
  '2person_aoe': SIZE64,
  '3person_aoe': SIZE64,
  '4person_aoe': SIZE64,
}

//tab3
export const tab3: IconMap = {
  small_enemy: SIZE64,
  medium_enemy: SIZE64,
  large_enemy: SIZE64,
  enhancement: SIZE32,
  enfeeblement: SIZE32,
  attack_1: SIZE32,
  attack_2: SIZE32,
  attack_3: SIZE32,
  attack_4: SIZE32,
  attack_5: SIZE32,
  attack_6: SIZE32,
  attack_7: SIZE32,
  attack_8: SIZE32,
  bind_1: SIZE32,
  bind_2: SIZE32,
  bind_3: SIZE32,
  ignore_1: SIZE32,
  ignore_2: SIZE32,
  square_marker: SIZE32,
  circle_marker: SIZE32,
  plus_marker: SIZE32,
  triangle_marker: SIZE32,
  waymark_a: SIZE48,
  waymark_b: SIZE48,
  waymark_c: SIZE48,
  waymark_d: SIZE48,
  waymark_1: SIZE48,
  waymark_2: SIZE48,
  waymark_3: SIZE48,
  waymark_4: SIZE48,
  lockon_red: SIZE48,
  lockon_blue: SIZE48,
  lockon_purple: SIZE48,
  lockon_green: SIZE48,
}

const tab3_size: Record<string, number> = {
  small_enemy: SIZE64,
  medium_enemy: SIZE64,
  large_enemy: SIZE64,
  enhancement: SIZE64,
  enfeeblement: SIZE64,
  attack_1: SIZE48,
  attack_2: SIZE48,
  attack_3: SIZE48,
  attack_4: SIZE48,
  attack_5: SIZE48,
  attack_6: SIZE48,
  attack_7: SIZE48,
  attack_8: SIZE48,
  bind_1: SIZE48,
  bind_2: SIZE48,
  bind_3: SIZE48,
  ignore_1: SIZE48,
  ignore_2: SIZE48,
  square_marker: SIZE48,
  circle_marker: SIZE48,
  plus_marker: SIZE48,
  triangle_marker: SIZE48,
  waymark_a: SIZE48,
  waymark_b: SIZE48,
  waymark_c: SIZE48,
  waymark_d: SIZE48,
  waymark_1: SIZE48,
  waymark_2: SIZE48,
  waymark_3: SIZE48,
  waymark_4: SIZE48,
  lockon_red: SIZE48,
  lockon_blue: SIZE48,
  lockon_purple: SIZE48,
  lockon_green: SIZE48,
}

//tab4
export const tab4: IconMap = {
  shape_circle: SIZE48,
  shape_x: SIZE48,
  shape_triangle: SIZE48,
  shape_square: SIZE48,
  up_arrow: SIZE48,
  rotate: SIZE48,
  highlighted_circle: SIZE48,
  highlighted_x: SIZE48,
  highlighted_square: SIZE48,
  highlighted_triangle: SIZE48,
  rotate_clockwise: SIZE48,
  rotate_counterclockwise: SIZE48,
}

// tab5
export const tab5: IconMap = {
  checkered_circle: SIZE256,
  checkered_square: SIZE256,
  grey_circle: SIZE256,
  grey_square: SIZE256,
}

interface JobIconConfig {
  src: string
  crop: IRect
  size: number
}

function easySpriteCrop(
  imageName: string,
  spriteWidth: number,
  itemNames: IconMap,
  spriteSizeMap?: Record<string, number>
): Record<string, JobIconConfig> {
  const result: Record<string, JobIconConfig> = {}
  let leftOffset = 0
  Object.entries(itemNames).forEach(([name, size], index) => {
    const left = spriteSizeMap ? leftOffset : spriteWidth * index
    const width = spriteSizeMap ? spriteSizeMap[name] * 2 : spriteWidth
    result[name] = {
      src: imageName,
      crop: {
        x: left,
        y: 0,
        width: width,
        height: width,
      },
      size: size,
    }
    if (spriteSizeMap) {
      leftOffset += width
    }
  })

  return result
}

const iconMap: Partial<Record<IconType, JobIconConfig>> = {
  //tab1
  ...easySpriteCrop('tab1', 128, tab1),

  //tab2
  ...easySpriteCrop('tab2', 512, tab2),

  //tab3
  ...easySpriteCrop('tab3', 128, tab3, tab3_size),
  //tab4
  ...easySpriteCrop('tab4', 96, tab4),
  //tab5
  ...easySpriteCrop('tab5', 512, tab5),
}

export function getIconConfig(data: StrategyObject): JobIconConfig | undefined {
  return iconMap[data.type as IconType]
}
