<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'

const data = ref<any>(null)
const checked = ref<Set<number>>(new Set())
const failAfter = ref<number | null>(1)
const busy = ref(false)
const error = ref('')
const flash = ref('')

function errText(e: any): string {
  const raw = String(e?.message || e)
  try { return JSON.parse(raw).detail ?? raw } catch { return raw }
}

async function refresh() {
  data.value = await api('/refills/latest?location_id=1')
  // 只保留仍可核销的勾选
  checked.value = new Set([...checked.value].filter(id =>
    data.value.lines.find((l: any) => l.lane_id === id)?.line_status === 'pending'))
}

const pendingLines = computed(() => (data.value?.lines || []).filter((l: any) => l.line_status === 'pending'))

function toggle(l: any) {
  if (l.line_status !== 'pending') return
  const next = new Set(checked.value)
  next.has(l.lane_id) ? next.delete(l.lane_id) : next.add(l.lane_id)
  checked.value = next
}

async function run() {
  busy.value = true; error.value = ''; flash.value = ''
  try {
    const d = await api('/refills/run?location_id=1', { method: 'POST' })
    flash.value = d.id === data.value?.id ? '已有进行中的补货单，无需重复生成（在途不重复占用）' : '已生成新补货单，补量已占用在途'
    await refresh()
  } catch (e) { error.value = errText(e) } finally { busy.value = false }
}

async function verify(ids: number[] | null, inject = false) {
  busy.value = true; error.value = ''; flash.value = ''
  try {
    const body: any = { lane_ids: ids }
    if (inject) body.fail_after = failAfter.value
    const d = await api(`/refills/${data.value.id}/verify`, {
      method: 'POST', body: JSON.stringify(body),
    })
    checked.value = new Set()
    flash.value = d.completed ? '本批核销成功，全部行已到货核销，整单完成' : '本批核销成功，仍有待核销行，整单未完成'
    await refresh()
  } catch (e) {
    error.value = inject
      ? `已注入失败并触发整批回滚（勾选行的库存/在途与行状态全部回到批前）：${errText(e)}`
      : errText(e)
    await refresh()
  } finally { busy.value = false }
}

async function voidOrder() {
  busy.value = true; error.value = ''; flash.value = ''
  try {
    await api(`/refills/${data.value.id}/void`, { method: 'POST' })
    flash.value = '整单已作废，待核销行占用的在途已释放'
    await refresh()
  } catch (e) { error.value = errText(e) } finally { busy.value = false }
}

function lineTag(l: any) {
  return l.line_status === 'verified' ? '已核销'
    : l.line_status === 'pending' ? '待核销'
    : l.line_status === 'voided' ? '已作废'
    : l.status === 'full' ? '满仓' : '超占'
}

onMounted(refresh)
</script>
<template>
  <h1>补货小票</h1>
  <p class="sub">分批到货按行核销 · 入选行加库存扣在途，未入选行继续占用在途 · 整批原子</p>

  <div class="card" v-if="data" style="margin-bottom:1rem;padding:0.8rem 1rem">
    <div style="display:flex;flex-wrap:wrap;gap:1rem;align-items:center">
      <strong>整单 #{{ data.id }}</strong>
      <span class="vf-badge" :class="data.completed ? 'vf-done' : data.voided ? 'vf-void' : 'vf-draft'">
        {{ data.completed ? '已完成' : data.voided ? '已作废' : '进行中（未完成）' }}
      </span>
      <span class="muted">正补量行 {{ data.verified_count }}/{{ data.total_line_count }} 已核销</span>
      <span class="muted">待核销补量 {{ data.pending_fill }} · 已入库 {{ data.verified_fill }}</span>
    </div>
  </div>

  <div style="display:flex;flex-wrap:wrap;gap:0.5rem;margin-bottom:0.8rem">
    <button class="btn" :disabled="busy || data?.voided" @click="run">生成/查看补货单</button>
    <button class="btn" :disabled="busy || pendingLines.length === 0 || checked.size === 0"
            @click="verify([...checked])">核销勾选行（{{ checked.size }}）</button>
    <button class="btn" :disabled="busy || pendingLines.length === 0" @click="verify(null)">全部到货核销</button>
    <button class="btn vf-btn-void" :disabled="busy || !data || data.completed || data.voided"
            @click="voidOrder">作废整单</button>
  </div>

  <div class="card" style="margin-bottom:0.8rem;padding:0.7rem 1rem">
    <strong>故障演练：</strong>
    对勾选行核销时，成功应用
    <input v-model.number="failAfter" type="number" min="0" style="width:4rem" />
    行后、提交前注入失败（0 = 首行前失败）→ 观察批内全回滚
    <button class="btn" style="margin-left:0.5rem"
            :disabled="busy || checked.size === 0 || failAfter === null"
            @click="verify([...checked], true)">注入失败并核销</button>
  </div>

  <p v-if="flash" class="vf-flash">{{ flash }}</p>
  <p v-if="error" class="vf-error">{{ error }}</p>

  <div style="margin-top:1rem" v-if="data">
    <div class="vf-receipt wide">
      <h2>*** VendFill 补货单 ***</h2>
      <div class="vf-receipt-line" style="font-weight:700;border-bottom:2px dashed #8a7e64">
        <span>货道 / 商品</span><span>库存·在途</span><span>补量</span><span>状态</span>
      </div>
      <label class="vf-receipt-line" v-for="l in data.lines" :key="l.lane_id"
             :class="{ 'vf-checkable': l.line_status === 'pending' }">
        <span>
          <input v-if="l.line_status === 'pending'" type="checkbox"
                 :checked="checked.has(l.lane_id)" @change="toggle(l)" />
          {{ l.slot_no }} {{ l.sku_name }}
        </span>
        <span>{{ l.stock }} · {{ l.in_transit }}</span>
        <span>{{ l.fill_qty }} / 缺{{ l.gap }}</span>
        <span :class="'vf-tag-' + l.line_status">{{ lineTag(l) }}</span>
      </label>
      <p style="text-align:center;margin:1rem 0 0;font-size:0.72rem;color:#6a5e48">
        待核销行补量仍计入在途 · 核销即入库
      </p>
    </div>
  </div>
</template>
