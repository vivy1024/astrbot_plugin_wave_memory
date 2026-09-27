import { describe, expect, it } from 'vitest'

import { isDirtyDisplayName, pickDisplayName, sanitizeDisplayName } from '@/lib/display-name'

const PROTOBUF_CARD = '\n\x11\x12\x0f猫猫副队长\n\t\n\x07$ÿĀ\x11\x10\x10\x00'

describe('sanitizeDisplayName', () => {
  it('从 protobuf 残片里取出主体昵称', () => {
    expect(sanitizeDisplayName(PROTOBUF_CARD)).toBe('猫猫副队长')
    expect(sanitizeDisplayName('zz[$ÿĀ\x11\x10]')).toBe('zz')
  })

  it('不误伤正常昵称', () => {
    for (const name of ['猫猫副队长', 'さくら🌸', 'Tom & Jerry', '★彡Kira彡★', 'Ｏ(≧▽≦)Ｏ', '[LM导入]', '👨‍👩‍👧 家族', 'José', '唱完山歌唱 帅哥ˇ']) {
      expect(sanitizeDisplayName(name)).toBe(name)
    }
  })

  it('折叠空白并去首尾空白', () => {
    expect(sanitizeDisplayName('  a   b ')).toBe('a b')
    expect(sanitizeDisplayName('a\tb\nc')).toBe('a b c')
    expect(sanitizeDisplayName('　　')).toBe('')
  })

  it('删除替换字符，控制字符切段取可读段', () => {
    expect(sanitizeDisplayName('岸本�的锅')).toBe('岸本的锅')
    expect(sanitizeDisplayName('José\x00')).toBe('José')
    expect(sanitizeDisplayName('abc\x00defg')).toBe('defg')
    expect(sanitizeDisplayName('\x00\x01')).toBe('')
    expect(sanitizeDisplayName(null)).toBe('')
  })
})

describe('pickDisplayName', () => {
  it('优先干净字段，都脏时用清洗结果，最后回退', () => {
    expect(isDirtyDisplayName(PROTOBUF_CARD)).toBe(true)
    expect(isDirtyDisplayName('猫猫副队长')).toBe(false)
    expect(pickDisplayName([PROTOBUF_CARD, '猫猫副队长'], '10000002')).toBe('猫猫副队长')
    expect(pickDisplayName([PROTOBUF_CARD, ''], '10000002')).toBe('猫猫副队长')
    expect(pickDisplayName(['', null], '10000002')).toBe('10000002')
    expect(pickDisplayName(['阿伟', '伟哥'], 'x')).toBe('阿伟')
  })
})
