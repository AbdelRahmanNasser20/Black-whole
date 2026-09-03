/* admin_map.js — shared Leaflet map for the admin dashboard.
 *
 * Self-contained module in the deal_card.js style: no build step, injects its
 * own CSS, exposes one global (window.AdminMap). Leaflet + markercluster load
 * lazily from cdnjs the first time a map is mounted, so tabs that never open
 * a map pay nothing. Basemap is CARTO dark_matter (keyless) to match the
 * admin's dark theme.
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

  const CSS = `
    .admin-map-box { position: relative; }
    .admin-map-box .leaflet-container {
      background: #14161c; border-radius: 10px; outline: none;
      font: inherit;
    }
    .admin-map-box .leaflet-popup-content-wrapper,
    .admin-map-box .leaflet-popup-tip {
      background: #1d2027; color: #e6e6e6;
      box-shadow: 0 6px 24px rgba(0,0,0,.5);
    }
    .admin-map-box .leaflet-popup-content { margin: 10px 14px; font-size: 13px; }
    .admin-map-box .leaflet-popup-content a { color: #7ab7ff; }
    .admin-map-box .leaflet-bar a {
      background: #1d2027; color: #e6e6e6; border-color: #333;
    }
    .admin-map-box .leaflet-bar a:hover { background: #2a2e37; }
    .admin-map-box .leaflet-control-attribution {
      background: rgba(20,22,28,.7); color: #888;
    }
    .admin-map-box .leaflet-control-attribution a { color: #aaa; }
    .amap-cluster {
      background: rgba(38,110,255,.85); color: #fff; border-radius: 50%;
      display: flex; align-items: center; justify-content: center;
      font-weight: 600; font-size: 12px;
      border: 2px solid rgba(255,255,255,.65);
      box-shadow: 0 2px 8px rgba(0,0,0,.45);
    }
    .amap-pin {
      background: #2f7dff; border: 2px solid #fff; border-radius: 50%;
      box-shadow: 0 1px 5px rgba(0,0,0,.5);
    }
    .amap-pin.approx { background: #b98a2f; }
    .amap-popup-img {
      display: block; width: 100%; max-height: 150px; object-fit: cover;
      border-radius: 6px; margin-bottom: 6px; background: #14161c;
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
