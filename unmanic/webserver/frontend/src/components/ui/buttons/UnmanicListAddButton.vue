<template>
  <q-btn
    outline
    dense
    round
    color="secondary"
    icon="add"
    :aria-label="ariaLabel || tooltip || undefined"
    :title="ariaLabel || tooltip || undefined"
    @click="$emit('click', $event)"
  >
    <q-tooltip v-if="tooltip" class="bg-white text-primary">
      {{ tooltip }}
    </q-tooltip>
  </q-btn>
</template>

<script setup>
defineProps({
  tooltip: {
    type: String,
    default: ''
  },
  ariaLabel: {
    type: String,
    default: ''
  }
})

defineEmits(['click'])
</script>
