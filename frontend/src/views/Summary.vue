<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api } from '../api'
const s = ref<any>({})
onMounted(async () => { s.value = await api('/refills/summary?location_id=1') })
</script>
<template>
  <h1>汇总</h1>
  <p class="sub">本点位补货建议合计与按行核销进度（同一套原子口径）</p>
  <div class="card" v-if="s.order_id" style="margin-bottom:0.85rem;display:flex;align-items:center;gap:0.8rem;flex-wrap:wrap">
    <strong>补货单 #{{ s.order_id }}</strong>
    <span class="vf-badge" :class="s.completed ? 'vf-done' : s.voided ? 'vf-void' : 'vf-draft'">
      {{ s.completed ? '已完成' : s.voided ? '已作废' : '进行中（未完成）' }}
    </span>
    <span class="muted">已核销行 {{ s.verified_count }}/{{ s.total_line_count }}</span>
  </div>
  <div class="card grid" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:1rem">
    <div><div class="muted">建议补货总量</div><div class="stat">{{ s.total_fill }}</div></div>
    <div><div class="muted">已入库补量</div><div class="stat">{{ s.verified_fill }}</div></div>
    <div><div class="muted">待核销补量（占用在途）</div><div class="stat">{{ s.pending_fill }}</div></div>
    <div><div class="muted">作废释放补量</div><div class="stat">{{ s.released_fill }}</div></div>
  </div>
  <div class="card grid" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:1rem;margin-top:0.85rem">
    <div><div class="muted">待补货道</div><div class="stat">{{ s.need_fill_count }}</div></div>
    <div><div class="muted">满仓货道</div><div class="stat">{{ s.full_count }}</div></div>
    <div><div class="muted">超占货道</div><div class="stat">{{ s.overbooked_count }}</div></div>
    <div><div class="muted">待核销行</div><div class="stat">{{ s.pending_count }}</div></div>
  </div>
  <p v-if="s.completed" class="vf-flash">整单已完成：全部正补量行均已核销，库存与在途口径一致，无待核销行。</p>
</template>
