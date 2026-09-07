/* admin_map.js — shared Leaflet map for the admin dashboard.
 *
 * Self-contained module in the deal_card.js style: no build step, injects its
 * own CSS, exposes one global (window.AdminMap). Leaflet + markercluster load
 * lazily from cdnjs the first time a map is mounted, so tabs that never open
 * a map pay nothing. Basemap is Esri Dark Gray Canvas (keyless); clusters, pins and
 * popups are painted with the page's CSS tokens (amber accent, radius 0).
 *
 * Usage:
 *   const map = await AdminMap.mount(containerEl);
 *   map.setPoints([{lat, lng, popup: '<html>', approx: false}, …]);
 *   map.fit();
 *   map.onViewport(({bounds, visible}) => …);   // fires on pan/zoom (moveend)
 *   map.inBounds(point) → bool                  // current-viewport test
 */
(() => {
  'use strict';

  const LEAFLET_VER = '1.9.4';
  const CLUSTER_VER = '1.5.3';
  const CDN = 'https://cdnjs.cloudflare.com/ajax/libs';

  // Colours come from the page's tokens (static/ui/tokens.css on the rebuilt pages; app.css legacy names on
  // the admin until E lands) — no literal hex here. Radius is 0 everywhere (plan §1.2), map container included.
  const CSS = `
    .admin-map-box { position: relative; border-radius: var(--radius, 0); }
    .admin-map-box.leaflet-container {   /* same element: mount() adds admin-map-box, L.map() adds leaflet-container */
      background: var(--surface, var(--bg-elev)); border-radius: var(--radius, 0); outline: none;
      font: inherit;
    }
    .admin-map-box .leaflet-popup-content-wrapper,
    .admin-map-box .leaflet-popup-tip {
      background: var(--surface-2, var(--bg-elev-2)); color: var(--text, var(--ink));
      border: 1px solid var(--border-strong, var(--line-bold)); border-radius: var(--radius, 0);
      box-shadow: var(--shadow-overlay, none);
    }
    .admin-map-box .leaflet-popup-tip { border-top: 0; border-left: 0; }
    .admin-map-box .leaflet-popup-content { margin: 10px 14px; font: 400 var(--fs-sm, 12px)/1.45 var(--sans, system-ui, sans-serif); }
    .admin-map-box .leaflet-popup-content a { color: var(--info); }
    .admin-map-box .leaflet-popup-content .mono { font-family: var(--mono); font-variant-numeric: tabular-nums; }
    .admin-map-box .leaflet-popup-close-button { color: var(--muted, var(--ink-mute)); }
    .admin-map-box .leaflet-popup-close-button:hover { color: var(--text, var(--ink)); }
    .admin-map-box .leaflet-bar { border: 1px solid var(--border-strong, var(--line-bold)); border-radius: var(--radius, 0); box-shadow: none; }
    .admin-map-box .leaflet-bar a {
      background: var(--surface-2, var(--bg-elev-2)); color: var(--text, var(--ink));
      border-bottom-color: var(--border, var(--line)); border-radius: var(--radius, 0);
      font-family: var(--mono);
    }
    .admin-map-box .leaflet-bar a:hover { background: var(--accent); color: var(--bg); }
    .admin-map-box .leaflet-control-attribution {
      background: var(--bg); color: var(--dim, var(--ink-dim)); font: 400 var(--fs-xs, 11px)/1.4 var(--mono);
    }
    .admin-map-box .leaflet-control-attribution a { color: var(--muted, var(--ink-mute)); }
    .leaflet-marker-icon.amap-cluster {   /* beats leaflet.css's display:block on .leaflet-marker-icon */
      display: flex; align-items: center; justify-content: center;
      background: linear-gradient(var(--accent-soft, color-mix(in srgb, var(--accent) 10%, transparent)),
                                  var(--accent-soft, color-mix(in srgb, var(--accent) 10%, transparent))), var(--bg);
      color: var(--text, var(--ink)); border: 1px solid var(--accent); border-radius: var(--radius, 0);
      font: 500 12px/1 var(--mono); font-variant-numeric: tabular-nums; box-shadow: none;
    }
    .amap-cluster:hover { background: var(--accent); color: var(--bg); }
    .amap-pin {
      background: var(--accent); border: 1px solid var(--bg); border-radius: var(--radius, 0);
      box-shadow: 0 0 0 1px var(--accent);
    }
    .amap-pin.approx { background: var(--surface-2, var(--bg-elev-2)); box-shadow: 0 0 0 1px var(--warn); border-color: var(--warn); }
    .amap-popup-img {
      display: block; width: 100%; max-height: 150px; object-fit: cover;
      border-radius: var(--radius, 0); margin-bottom: 6px; background: var(--surface, var(--bg-elev));
    }
  `;

  let loadPromise = null;

  function injectOnce(tag, attrs) {
    return new Promise((resolve, reject) => {
      const el = document.createElement(tag);
      Object.assign(el, attrs);
      el.onload = resolve;
      el.onerror = () => reject(new Error('failed to load ' + (attrs.src || attrs.href)));
      document.head.appendChild(el);
    });
  }

  function loadLibs() {
    if (loadPromise) return loadPromise;
    const style = document.createElement('style');
    style.textContent = CSS;
    document.head.appendChild(style);
    loadPromise = (async () => {
      await Promise.all([
        injectOnce('link', { rel: 'stylesheet', href: `${CDN}/leaflet/${LEAFLET_VER}/leaflet.min.css` }),
        injectOnce('link', { rel: 'stylesheet', href: `${CDN}/leaflet.markercluster/${CLUSTER_VER}/MarkerCluster.min.css` }),
      ]);
      await injectOnce('script', { src: `${CDN}/leaflet/${LEAFLET_VER}/leaflet.min.js` });
      await injectOnce('script', { src: `${CDN}/leaflet.markercluster/${CLUSTER_VER}/leaflet.markercluster.min.js` });
      return window.L;
    })();
    return loadPromise;
  }

  async function mount(container) {
    const L = await loadLibs();
    container.classList.add('admin-map-box');

    const map = L.map(container, {
      center: [39.5, -98.35], // continental US
      zoom: 4,
      worldCopyJump: true,
    });
    // Esri Dark Gray Canvas: keyless, no watermark (CARTO free tiles now
    // stamp "API KEY REQUIRED" across every tile).
    L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}', {
      attribution: 'Tiles &copy; Esri &mdash; Esri, DeLorme, NAVTEQ',
      maxZoom: 16,
    }).addTo(map);

    const cluster = L.markerClusterGroup({
      showCoverageOnHover: false,
      chunkedLoading: true,
      maxClusterRadius: 55,
      iconCreateFunction: (c) => {
        const n = c.getChildCount();
        const size = n >= 1000 ? 44 : n >= 100 ? 38 : 32;
        const label = n >= 1000 ? (Math.round(n / 100) / 10) + 'k' : String(n);
        return L.divIcon({
          html: label, className: 'amap-cluster',
          iconSize: L.point(size, size),
        });
      },
    });
    map.addLayer(cluster);

    let points = [];
    const viewportCbs = [];

    const inBounds = (p) =>
      p.lat != null && p.lng != null && map.getBounds().contains([p.lat, p.lng]);

    map.on('moveend zoomend', () => {
      const visible = points.filter(inBounds).length;
      viewportCbs.forEach((cb) => cb({ bounds: map.getBounds(), visible }));
    });

    return {
      leaflet: map,

      setPoints(next) {
        points = (next || []).filter((p) => p.lat != null && p.lng != null);
        cluster.clearLayers();
        cluster.addLayers(points.map((p) => {
          const m = L.marker([p.lat, p.lng], {
            icon: L.divIcon({
              className: 'amap-pin' + (p.approx ? ' approx' : ''),
              iconSize: [14, 14],
            }),
            title: p.title || '',
          });
          if (p.popup) m.bindPopup(p.popup, { maxWidth: 280 });
          return m;
        }));
      },

      fit() {
        if (!points.length) return;
        const b = L.latLngBounds(points.map((p) => [p.lat, p.lng]));
        map.fitBounds(b.pad(0.1), { maxZoom: 11 });
      },

      onViewport(cb) { viewportCbs.push(cb); },
      inBounds,
      // "south,west,north,east" for the /api/deals bbox param.
      bboxParam() {
        const b = map.getBounds();
        return [b.getSouth(), b.getWest(), b.getNorth(), b.getEast()]
          .map((v) => v.toFixed(4)).join(',');
      },
      invalidateSize() { map.invalidateSize(); },
      count() { return points.length; },
      visibleCount() { return points.filter(inBounds).length; },
    };
  }

  window.AdminMap = { mount };
})();
