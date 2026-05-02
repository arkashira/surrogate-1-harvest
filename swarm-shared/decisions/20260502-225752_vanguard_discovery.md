# vanguard / discovery

## 1. Diagnosis

- Dataset detail view (`#/datasets/:id`) does not reflect filter/pagination in URL — reloads or shared links lose context.
- No canonical route for dataset detail; deep links 404 or fall back to list view.
- Missing loading/error UI on dataset fetch → blank screen or silent failure.
- Scroll position resets on navigation into detail view → disorienting jumps.
- No back-to-list affordance or breadcrumb; user loses context after drilling in.

## 2. Proposed change

File: `/opt/axentx/vanguard/src/routes.js` (or equivalent router config)  
Scope: add canonical dataset detail route, persist filter/pagination in URL query params, wire loading/error boundary, and restore scroll per navigation.

If routes are defined elsewhere (e.g., inline in main app file), apply the same pattern to that file.

## 3. Implementation

```js
// /opt/axentx/vanguard/src/routes.js
import { createBrowserRouter, RouterProvider, useSearchParams, useNavigation } from 'react-router-dom';
import { useEffect, useState } from 'react';
import DatasetList from './pages/DatasetList';
import DatasetDetail from './pages/DatasetDetail';
import Loading from './components/Loading';
import ErrorBoundary from './components/ErrorBoundary';

// Helper: scroll restoration on route change
function ScrollRestoration() {
  const navigation = useNavigation();
  useEffect(() => {
    if (navigation.location) {
      window.scrollTo(0, 0);
    }
  }, [navigation.location]);
  return null;
}

// Dataset list with persisted filters/pagination in URL
function DatasetListRoute() {
  const [searchParams, setSearchParams] = useSearchParams();
  const page = searchParams.get('page') || '1';
  const filter = searchParams.get('filter') || '';
  const sort = searchParams.get('sort') || 'relevance';

  const setFilters = (opts) => {
    const next = new URLSearchParams(searchParams);
    if (opts.page !== undefined) next.set('page', String(opts.page));
    if (opts.filter !== undefined) next.set('filter', opts.filter);
    if (opts.sort !== undefined) next.set('sort', opts.sort);
    setSearchParams(next, { replace: true });
  };

  return (
    <ErrorBoundary>
      <Loading>
        <DatasetList
          page={Number(page)}
          filter={filter}
          sort={sort}
          onPageChange={(p) => setFilters({ page: p })}
          onFilterChange={(f) => setFilters({ filter: f, page: 1 })}
          onSortChange={(s) => setFilters({ sort: s })}
        />
      </Loading>
    </ErrorBoundary>
  );
}

// Dataset detail with canonical route and back-to-list affordance
function DatasetDetailRoute() {
  const [searchParams] = useSearchParams();
  // preserve list context in query so detail can link back with same filters
  const listSearch = searchParams.toString();
  return (
    <ErrorBoundary>
      <Loading>
        <DatasetDetail listSearch={listSearch} />
      </Loading>
    </ErrorBoundary>
  );
}

const router = createBrowserRouter([
  {
    path: '/',
    element: (
      <>
        <ScrollRestoration />
        <DatasetListRoute />
      </>
    ),
    errorElement: <ErrorBoundary />,
  },
  {
    path: '/datasets/:id',
    element: (
      <>
        <ScrollRestoration />
        <DatasetDetailRoute />
      </>
    ),
    errorElement: <ErrorBoundary />,
  },
  // Redirect legacy hash routes to canonical paths (if needed)
  {
    path: '/#/datasets/:id',
    loader: ({ params }) => {
      // client-side redirect
      window.location.replace(`/datasets/${params.id}`);
      return null;
    },
  },
]);

export default function AppRouter() {
  return <RouterProvider router={router} />;
}
```

If the project uses a different framework (e.g., Vue, Svelte, or vanilla), apply equivalent changes:

- Add route `/datasets/:id` that maps to dataset detail component.
- Encode `page`, `filter`, `sort` as URL query parameters and read/write them on filter/pagination changes.
- Add a lightweight loading skeleton and error boundary around async dataset fetch.
- Call `window.scrollTo(0, 0)` on route change (or use framework-native scroll behavior).
- Provide a “Back to list” link that preserves query params: `/datasets/123?listSearch=page%3D2%26filter%3Dpublic`.

## 4. Verification

1. Start dev server and navigate to `/`.
2. Change page/filter/sort and confirm URL updates (e.g., `/?page=2&filter=public&sort=date`).
3. Click a dataset to open `/datasets/abc123`. Confirm:
   - Detail loads with correct data.
   - URL is canonical (no hash).
   - Scroll resets to top.
4. Copy the detail URL and open in a new tab — detail renders directly.
5. Use browser back/forward — filters/pagination in list persist and scroll does not jump unexpectedly.
6. Simulate slow network (DevTools) — loading UI appears; simulate 404/500 — error boundary shows message.
