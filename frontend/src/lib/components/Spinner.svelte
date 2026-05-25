<script lang="ts">
  interface Props {
    size?: number;
  }
  let { size = 18 }: Props = $props();
</script>

<span
  class="inline-block animate-spin rounded-full border-2 border-border border-t-primary"
  style:width="{size}px"
  style:height="{size}px"
></span>
