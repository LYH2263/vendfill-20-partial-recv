<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api } from '../api'
const rows = ref<any[]>([])
const refill = ref<any>(null)
onMounted(async () => {
  // 先取补货单（首次可能开单并预占在途），再取货道，避免显示批前口径
  try { refill.value = await api('/refills/latest?location_id=1') } catch { /* 无补货单时忽略 */ }
  rows.value = await api('/lanes')
})
function tag(l: any) {
  if (l.fill_qty === 0) return l.status === 'full' ? '满仓' : '超占'
  return l.line_status === 'verified' ? '已核销' : l.line_status === 'voided' ? '已作废' : '待核销'
}
</script>
<template>
  <h1>货道格子</h1>
  <p class="sub">机面货道网格 · 格内库存条 · 右侧补货小票</p>
  <div class="vf-machine-layout">
    <div class="vf-slot-grid">
      <div v-for="r in rows" :key="r.id" class="vf-slot">
        <div class="vf-slot-no">{{ r.slot_no }}</div>
        <div class="vf-slot-sku">{{ r.sku_name }}</div>
        <div class="vf-slot-bar">
          <div
            class="vf-slot-fill"
            :class="{ 'vf-need': r.gap > 0 }"
            :style="{ width: Math.min(r.fill_pct, 100) + '%' }"
          />
        </div>
        <div class="vf-slot-meta">{{ r.stock }}/{{ r.capacity }} · 在途 {{ r.in_transit }} · 缺 {{ r.gap }}</div>
      </div>
    </div>
    <aside class="vf-receipt" v-if="refill">
      <h2>*** 补货建议单 #{{ refill.id }} ***</h2>
      <div class="vf-receipt-line" v-for="l in refill.lines" :key="l.lane_id">
        <span>{{ l.slot_no }} {{ l.sku_name }} <small>({{ tag(l) }})</small></span>
        <span>x{{ l.fill_qty }}</span>
      </div>
      <p class="muted" style="margin:0.75rem 0 0;font-size:0.72rem;color:#6a5e48;text-align:center">
        {{ refill.status === 'completed' ? '— 整单已完成 —' : refill.status === 'voided' ? '— 已作废 —' : '— 进行中：可分批核销 —' }}
      </p>
    </aside>
  </div>
</template>
