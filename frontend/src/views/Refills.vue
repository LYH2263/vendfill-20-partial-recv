<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'

interface Line {
  lane_id: number
  slot_no: string
  sku_name: string
  fill_qty: number
  gap: number
  status: string
  line_status: string
  lane_stock: number | null
  lane_in_transit: number | null
}
interface OrderData {
  id: number
  status: 'active' | 'completed' | 'voided'
  total_fill: number
  pending_count: number
  verified_count: number
  voided_count: number
  pending_qty: number
  verified_qty: number
  all_verified: boolean
  lines: Line[]
}

const data = ref<OrderData | null>(null)
const lanes = ref<any[]>([])
const checked = ref<Set<number>>(new Set())
const error = ref('')
const loading = ref(false)

const positiveLines = computed(() => (data.value?.lines ?? []).filter(l => l.fill_qty > 0))
const selectable = computed(() =>
  positiveLines.value.filter(l => l.line_status === 'pending' && data.value?.status === 'active'))

async function refresh() {
  try {
    data.value = await api('/refills/latest?location_id=1')
    lanes.value = await api('/lanes')
  } catch (e: any) {
    error.value = parseError(e.message)
  }
  checked.value = new Set()
}

function toggle(l: Line) {
  if (l.fill_qty <= 0 || l.line_status !== 'pending' || data.value?.status !== 'active') return
  const next = new Set(checked.value)
  if (next.has(l.lane_id)) next.delete(l.lane_id); else next.add(l.lane_id)
  checked.value = next
}

function selectAll() { checked.value = new Set(selectable.value.map(l => l.lane_id)) }
function toggleAll(e: Event) {
  if ((e.target as HTMLInputElement).checked) selectAll()
  else checked.value = new Set()
}

async function verifySelected() {
  if (!data.value || checked.value.size === 0) return
  loading.value = true; error.value = ''
  try {
    data.value = await api(`/refills/${data.value.id}/verify`, {
      method: 'POST', body: JSON.stringify({ lane_ids: [...checked.value] }),
    })
    checked.value = new Set()
    lanes.value = await api('/lanes')
  } catch (e: any) {
    error.value = parseError(e.message)
  } finally { loading.value = false }
}

async function voidOrder() {
  if (!data.value || !confirm('作废本单？待核销行的在途占用将全部释放。')) return
  loading.value = true; error.value = ''
  try {
    data.value = await api(`/refills/${data.value.id}/void`, { method: 'POST' })
    lanes.value = await api('/lanes')
  } catch (e: any) {
    error.value = parseError(e.message)
  } finally { loading.value = false }
}

async function newOrder() {
  loading.value = true; error.value = ''
  try {
    data.value = await api('/refills/run?location_id=1', { method: 'POST' })
    checked.value = new Set()
    lanes.value = await api('/lanes')
  } catch (e: any) {
    error.value = parseError(e.message)
  } finally { loading.value = false }
}

function parseError(raw: string) {
  try { return JSON.parse(raw).detail ?? raw } catch { return raw }
}

function lineTag(l: Line) {
  if (l.fill_qty === 0) return l.status === 'full' ? '满仓' : '超占'
  return l.line_status === 'verified' ? '已核销' : l.line_status === 'voided' ? '已作废' : '待核销'
}
function statusText(s: string) {
  return s === 'active' ? '进行中' : s === 'completed' ? '整单完成' : '已作废'
}

onMounted(refresh)
</script>
<template>
  <h1>补货小票 · 分批核销</h1>
  <p class="sub">到货后勾选行核销：加库存、扣在途；整单仅当全部待补行核销后才完成</p>

  <div v-if="error" class="card" style="border-color:var(--vf-red);color:var(--vf-red);font-size:0.8rem">
    ⚠ {{ error }}（本批未提交，行状态与货道均未改动）
  </div>

  <div v-if="data" class="vf-machine-layout">
    <div class="vf-receipt">
      <h2>*** VendFill 补货单 #{{ data.id }} ***</h2>

      <div class="vf-receipt-line" style="border-bottom:2px dashed #8a7e64;font-weight:700">
        <span>整单状态：{{ statusText(data.status) }}</span>
        <span>{{ data.verified_count }}/{{ data.verified_count + data.pending_count + data.voided_count }} 行</span>
      </div>
      <div class="vf-receipt-line">
        <span>已核销量 / 待核销量</span>
        <span>{{ data.verified_qty }} / {{ data.pending_qty }}</span>
      </div>

      <div class="vf-receipt-line" style="font-weight:700;border-bottom:2px dashed #8a7e64">
        <span><label v-if="selectable.length && data.status === 'active'" style="cursor:pointer">
          <input type="checkbox" :checked="checked.size === selectable.length" @change="toggleAll"> 全选
        </label> 货道 / 商品</span>
        <span>补量 · 库存/在途</span>
      </div>

      <div v-for="l in data.lines" :key="l.lane_id" class="vf-receipt-line"
           :style="{ cursor: l.line_status === 'pending' && l.fill_qty > 0 && data.status === 'active' ? 'pointer' : 'default',
                     opacity: l.line_status === 'verified' ? 0.55 : 1 }"
           @click="toggle(l)">
        <span>
          <input v-if="l.fill_qty > 0" type="checkbox"
                 :checked="l.line_status === 'verified' || checked.has(l.lane_id)"
                 :disabled="l.line_status !== 'pending' || data.status !== 'active'"
                 style="margin-right:0.35rem" @click.stop @change="toggle(l)">
          {{ l.slot_no }} {{ l.sku_name }}
          <small>({{ lineTag(l) }})</small>
        </span>
        <span>{{ l.fill_qty }} · {{ l.lane_stock }}/{{ l.lane_in_transit }}</span>
      </div>

      <div style="margin-top:0.8rem;display:flex;gap:0.5rem;flex-wrap:wrap">
        <button class="btn" :disabled="loading || checked.size === 0 || data.status !== 'active'"
                @click.stop="verifySelected">
          核销勾选 {{ checked.size ? `(${checked.size})` : '' }}
        </button>
        <button class="btn" v-if="data.status === 'active'"
                style="background:var(--vf-amber)" :disabled="loading" @click.stop="voidOrder">
          作废整单
        </button>
        <button class="btn" v-else :disabled="loading" @click.stop="newOrder">
          按当前缺口重新开单
        </button>
      </div>
      <p style="text-align:center;margin:1rem 0 0;font-size:0.72rem;color:#6a5e48">
        任一行核销失败，本批全部回滚 · 未勾选行不动
      </p>
    </div>

    <aside class="card">
      <div style="font-weight:700;margin-bottom:0.5rem">货道实时口径</div>
      <table>
        <thead><tr><th>货道</th><th>库存</th><th>在途</th><th>容量</th></tr></thead>
        <tbody>
          <tr v-for="r in lanes" :key="r.id">
            <td>{{ r.slot_no }}</td><td>{{ r.stock }}</td><td>{{ r.in_transit }}</td><td>{{ r.capacity }}</td>
          </tr>
        </tbody>
      </table>
    </aside>
  </div>
</template>
