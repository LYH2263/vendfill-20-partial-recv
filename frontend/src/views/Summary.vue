<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api } from '../api'
const s = ref<any>(null)
const error = ref('')
onMounted(async () => {
  try { s.value = await api('/refills/summary?location_id=1') }
  catch (e: any) {
    try { error.value = JSON.parse(e.message).detail ?? e.message } catch { error.value = e.message }
  }
})
</script>
<template>
  <h1>汇总</h1>
  <p class="sub">本点位最新补货单 · 行核销态与货道库存/在途同一套口径</p>
  <div v-if="error" class="card" style="border-color:var(--vf-amber);color:var(--vf-amber);font-size:0.85rem">
    {{ error }}
  </div>
  <template v-else-if="s">
  <div class="card" style="display:flex;align-items:center;gap:0.6rem;font-size:0.85rem">
    <span class="muted">补货单 #{{ s.order_id }}</span>
    <span class="badge"
          :class="s.status === 'completed' ? 'badge-ok' : s.status === 'voided' ? 'badge-bad' : 'badge-warn'">
      {{ s.status === 'active' ? '进行中' : s.status === 'completed' ? '整单完成' : '已作废' }}
    </span>
    <span v-if="s.status === 'active'" class="muted">
      （已完成须待全部正补量行核销；当前已核销 {{ s.verified_count }} 行 / 待核销 {{ s.pending_count }} 行）
    </span>
  </div>
  <div class="card grid" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:1rem">
    <div><div class="muted">建议补货总量</div><div class="stat">{{ s.total_fill }}</div></div>
    <div><div class="muted">已核销量</div><div class="stat" style="color:var(--vf-led)">{{ s.verified_qty }}</div></div>
    <div><div class="muted">待核销量（在途占用）</div><div class="stat" style="color:var(--vf-amber)">{{ s.pending_qty }}</div></div>
    <div><div class="muted">待补/满仓/超占货道</div><div class="stat">{{ s.need_fill_count }}/{{ s.full_count }}/{{ s.overbooked_count }}</div></div>
  </div>
  </template>
</template>
