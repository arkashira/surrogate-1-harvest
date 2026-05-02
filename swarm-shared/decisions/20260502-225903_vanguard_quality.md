# vanguard / quality

## Final Synthesized Implementation

**Core principles adopted:**
- Hash router with **correct scroll restoration** (back/forward restore position; new navigations go to top; hash anchors scroll to element).
- **URL-driven state** for dataset list (page, pageSize, sort, dir, filter) and detail (id) so reloads and shared links work.
- **Route-level loading/error UI** to avoid blank/frozen screens.
- **Canonical routes** with 404 fallback and proper redirects.
- **Preserve scroll inside detail view** across param changes, but reset on list navigation.

---

### 1. Router (`src/router.js`)

```js
// src/router.js
import { createRouter, createWebHashHistory } from 'vue-router';
import DatasetList from './pages/DatasetList.vue';
import DatasetDetail from './pages/DatasetDetail.vue';
import NotFound from './pages/NotFound.vue';

function scrollBehavior(to, from, savedPosition) {
  // Back/forward: restore exact saved position
  if (savedPosition) {
    return savedPosition;
  }

  // Hash anchor: scroll to element
  if (to.hash) {
    try {
      const el = document.querySelector(decodeURIComponent(to.hash));
      if (el) {
        return { el, behavior: 'smooth', block: 'start' };
      }
    } catch (e) {
      // ignore invalid selector
    }
  }

  // Preserve scroll for routes that explicitly opt in
  if (to.matched.some((r) => r.meta.preserveScroll)) {
    return false;
  }

  // Default: scroll to top for new navigations
  return { top: 0, behavior: 'smooth' };
}

const routes = [
  {
    path: '/',
    redirect: { name: 'dataset-list' },
  },
  {
    path: '/datasets',
    name: 'dataset-list',
    component: DatasetList,
    // State stored in query: ?page=2&pageSize=20&sort=createdAt&dir=desc&filter=...
  },
  {
    path: '/datasets/:id',
    name: 'dataset-detail',
    component: DatasetDetail,
    props: true,
    meta: { preserveScroll: true },
  },
  {
    path: '/:pathMatch(.*)*',
    name: 'not-found',
    component: NotFound,
  },
];

const router = createRouter({
  history: createWebHashHistory(),
  routes,
  scrollBehavior,
});

export default router;
```

---

### 2. DatasetList (`src/pages/DatasetList.vue`)

Synchronizes filters/pagination/sort with URL query params and provides loading/error UI.

```vue
<template>
  <div>
    <div v-if="loading" class="loading">Loading datasets…</div>
    <div v-else-if="error" class="error">Failed to load datasets: {{ error }}</div>
    <DatasetTable
      v-else
      :items="items"
      :pagination="pagination"
      :filters="filters"
      :sort="sort"
      @update="onUpdate"
    />
  </div>
</template>

<script>
import { ref, watch, onMounted } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import DatasetTable from '../components/DatasetTable.vue';
import { fetchDatasets } from '../api/datasets';

export default {
  components: { DatasetTable },
  setup() {
    const route = useRoute();
    const router = useRouter();

    const loading = ref(false);
    const error = ref(null);
    const items = ref([]);

    const page = ref(1);
    const pageSize = ref(20);
    const sort = ref('createdAt');
    const dir = ref('desc');
    const filter = ref('');

    const pagination = { page, pageSize };
    const filters = { filter };
    const sortState = { sort, dir };

    function parseQuery() {
      const q = route.query || {};
      page.value = parseInt(q.page, 10) || 1;
      pageSize.value = parseInt(q.pageSize, 10) || 20;
      sort.value = q.sort || 'createdAt';
      dir.value = q.dir === 'asc' ? 'asc' : 'desc';
      filter.value = q.filter || '';
    }

    async function load() {
      loading.value = true;
      error.value = null;
      try {
        const res = await fetchDatasets({
          page: page.value,
          pageSize: pageSize.value,
          sort: sort.value,
          dir: dir.value,
          filter: filter.value,
        });
        items.value = res.items || [];
      } catch (err) {
        error.value = err.message || String(err);
      } finally {
        loading.value = false;
      }
    }

    function pushQuery() {
      router.replace({
        query: {
          page: page.value > 1 ? page.value : undefined,
          pageSize: pageSize.value !== 20 ? pageSize.value : undefined,
          sort: sort.value !== 'createdAt' ? sort.value : undefined,
          dir: dir.value !== 'desc' ? dir.value : undefined,
          filter: filter.value || undefined,
        },
      });
    }

    function onUpdate(update) {
      if (update.page != null) page.value = update.page;
      if (update.pageSize != null) pageSize.value = update.pageSize;
      if (update.sort != null) sort.value = update.sort;
      if (update.dir != null) dir.value = update.dir;
      if (update.filter != null) filter.value = update.filter;
      pushQuery();
    }

    // Re-load when query changes (deep link or back/forward)
    watch(
      () => route.query,
      () => {
        parseQuery();
        load();
      },
      { deep: true }
    );

    onMounted(() => {
      parseQuery();
      load();
    });

    return { loading, error, items, pagination, filters, sort: sortState, onUpdate };
  },
};
</script>

<style scoped>
.loading { padding: 1rem; color: #666; }
.error { padding: 1rem; color: #b00020; }
</style>
```

---

### 3. DatasetDetail (`src/pages/DatasetDetail.vue`)

Canonical route handling with loading/error UI and scroll preservation inside the detail view.

```vue
<template>
  <div>
    <div v-if="loading" class="loading">Loading dataset…</div>
    <div v-else-if="error" class="error">Failed to load dataset: {{ error }}</div>
    <div v-else-if="!item" class="error">Dataset not found.</div>
    <DatasetView v-else :item="item" />
  </div>
</template>

<script>
import { ref, watch, onMounted } from 'vue';
import { useRoute } from 'vue-router';
import DatasetView from '../components/DatasetView.vue';
import { fetchDataset } from '../api/datasets';

export default {
  components: { DatasetView },
  setup() {
    const route = useRoute();
    const loading = ref(false);
    const error = ref(null);
    const item = ref(null);

    async function load(id) {
      if (!id) return;
      loading.value = true;
      error.value = null;
      item.value = null;
      try {
        item.value = await fetchDataset(id);
      } catch (err) {
        error.value = err.message || String(err);
      } finally {
        loading.value = false;
      }
    }

    watch(
      () => route.params.id,
      (id) => load(id),
      { immediate: true }
    );

    onMounted(() => {
      load(route.params.id);
    });

    return { loading, error, item };
  },
};
</script>

<style scoped>
.loading { padding: 1rem; color: #666; }
.error { padding: 1rem; color: #b00020; }
</style>
```

---

### Key Resolutions

- **Scroll restoration**: Uses Vue Router’s `scrollBehavior
