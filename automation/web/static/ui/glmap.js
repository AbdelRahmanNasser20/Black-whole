// static/ui/glmap.js — the shared MapLibre GL map for the public pages (vector basemap, same look as the CRM map).
// Same handle as window.AdminMap (static/admin_map.js, Leaflet), so a page moves over by changing one import:
//   const map = await mount(containerEl, {styleUrl});   // rejects (err.mapFailed) when the library or style cannot load
//   map.setPoints([{lat, lng, popup: '<html>', title}, …]);  map.fit();
//   map.onViewport(({bounds, visible}) => …);           // fires on pan/zoom (moveend)
//   map.inBounds(point) · map.bboxParam() → 'S,W,N,E' · map.invalidateSize() · map.count() · map.visibleCount()
// MapLibre loads from cdnjs the first time a map mounts, so a page that never opens its map pays nothing.
// Points go in one clustered GeoJSON source: 5,000 pins are three GL layers, not 5,000 DOM nodes.
const CDN = 'https://cdnjs.cloudflare.com/ajax/libs/maplibre-gl/4.7.1/';   // pinned; bump on purpose
const DEFAULT_STYLE = 'https://tiles.openfreemap.org/styles/positron';
const LOAD_TIMEOUT_MS = 20000;
const SRC = 'glmap-points';
const POPUP_MAX = 10;           // lots listed in one popup when several share a point

// Popup + control chrome painted with the page tokens (the map itself is styled by the style URL).
const CSS = `
  .gl-map-box { position: relative; border-radius: var(--radius, 0); font: inherit; }
  .gl-map-box .maplibregl-popup-content {
    background: var(--surface-2); color: var(--text); border: 1px solid var(--border-strong, var(--border));
    border-radius: var(--radius, 0); padding: 10px 14px; font: 400 var(--fs-sm, 12px)/1.45 var(--sans, system-ui, sans-serif);
    box-shadow: var(--shadow-overlay, none); max-width: 280px;
  }
  .gl-map-box .maplibregl-popup-tip { display: none; }
  .gl-map-box .maplibregl-popup-content { max-height: 260px; overflow-y: auto; }
  .gl-map-box .gl-popup-item + .gl-popup-item { border-top: 1px solid var(--border); margin-top: 8px; padding-top: 8px; }
  .gl-map-box .gl-popup-more { color: var(--muted); margin: 8px 0 0; }
  .gl-map-box .maplibregl-popup-content a { color: var(--info, var(--accent)); }
  .gl-map-box .maplibregl-popup-content .mono { font-family: var(--mono); font-variant-numeric: tabular-nums; }
  .gl-map-box .maplibregl-popup-close-button { color: var(--muted); font-size: 16px; padding: 2px 6px; }
  .gl-map-box .maplibregl-ctrl-group { border-radius: var(--radius, 0); box-shadow: none; border: 1px solid var(--border-strong, var(--border)); }
`;

let libs = null;

function inject(tag, attrs) {
  return new Promise((resolve, reject) => {
    const el = document.createElement(tag);
    Object.assign(el, attrs);
    el.onload = resolve;
    el.onerror = () => reject(new Error('failed to load ' + (attrs.src || attrs.href)));
    document.head.appendChild(el);
  });
}

function failed(message) { const err = new Error(message); err.mapFailed = true; return err; }

function loadLibs() {
  if (libs) return libs;
  const style = document.createElement('style');
  style.textContent = CSS;
  document.head.appendChild(style);
  libs = Promise.all([
    inject('link', {rel: 'stylesheet', href: CDN + 'maplibre-gl.min.css'}),
    inject('script', {src: CDN + 'maplibre-gl.min.js'}),
  ]).then(() => {
    if (!window.maplibregl) throw new Error('maplibre-gl loaded without its global');
    return window.maplibregl;
  }).catch((err) => { libs = null; throw failed(err.message); });   // a later mount may try again
  return libs;
}

const token = (el, name, fallback) => getComputedStyle(el).getPropertyValue(name).trim() || fallback;

// The cluster number needs a font the style's glyph server has: reuse the first one the style itself names.
function styleFont(map) {
  for (const layer of map.getStyle().layers || []) {
    const f = layer.layout && layer.layout['text-font'];
    if (Array.isArray(f) && f.length && typeof f[0] === 'string') return f;
  }
  return null;
}

function addLayers(map, box) {
  const accent = token(box, '--accent', 'orange'), ink = token(box, '--bg', 'black');
  map.addSource(SRC, {type: 'geojson', data: {type: 'FeatureCollection', features: []},
                      cluster: true, clusterRadius: 50, clusterMaxZoom: 12});
  map.addLayer({id: 'glmap-clusters', type: 'circle', source: SRC, filter: ['has', 'point_count'], paint: {
    'circle-color': accent, 'circle-opacity': 0.9, 'circle-stroke-color': ink, 'circle-stroke-width': 1.5,
    'circle-radius': ['step', ['get', 'point_count'], 14, 100, 18, 1000, 23],
  }});
  const font = styleFont(map);
  if (font) map.addLayer({id: 'glmap-cluster-count', type: 'symbol', source: SRC, filter: ['has', 'point_count'],
    layout: {'text-field': ['get', 'point_count_abbreviated'], 'text-font': font, 'text-size': 12, 'text-allow-overlap': true},
    paint: {'text-color': ink}});
  map.addLayer({id: 'glmap-pins', type: 'circle', source: SRC, filter: ['!', ['has', 'point_count']], paint: {
    'circle-color': accent, 'circle-radius': 6, 'circle-stroke-color': ink, 'circle-stroke-width': 1.5,
  }});
}

export async function mount(container, opts = {}) {
  const gl = await loadLibs();
  container.classList.add('gl-map-box');
  let map;
  try {
    map = new gl.Map({
      container, style: opts.styleUrl || DEFAULT_STYLE,
      center: [-98.35, 39.5], zoom: 3,              // continental US
      attributionControl: {compact: true},
    });
  } catch (err) { throw failed(err.message || 'the map could not start'); }   // no WebGL

  await new Promise((resolve, reject) => {
    // Only a style that never arrives fails the map. One bad tile, sprite or glyph is an 'error' event too;
    // those leave a usable map, so they are ignored (they carry sourceId/tile, or come after the style).
    let styleArrived = false;
    const done = (fn, arg) => { clearTimeout(timer); map.off('error', onError); fn(arg); };
    const timer = setTimeout(() => (styleArrived ? done(resolve)
      : done(reject, failed('the map style timed out'))), LOAD_TIMEOUT_MS);
    const onError = (e) => {
      if (styleArrived || (e && (e.sourceId || e.tile))) return;
      done(reject, failed((e && e.error && e.error.message) || 'the map style failed to load'));
    };
    map.once('styledata', () => { styleArrived = true; });
    map.once('load', () => done(resolve));
    map.on('error', onError);
  }).catch((err) => { try { map.remove(); } catch (_) {} container.classList.remove('gl-map-box'); throw err; });

  map.addControl(new gl.NavigationControl({showCompass: false}), 'top-left');
  addLayers(map, container);

  let points = [];
  const viewportCbs = [];
  const inBounds = (p) => p.lat != null && p.lng != null && map.getBounds().contains([p.lng, p.lat]);

  map.on('moveend', () => {
    const visible = points.filter(inBounds).length;
    viewportCbs.forEach((cb) => cb({bounds: map.getBounds(), visible}));
  });
  map.on('click', 'glmap-clusters', async (e) => {
    const f = e.features && e.features[0];
    if (!f) return;
    try {
      const zoom = await map.getSource(SRC).getClusterExpansionZoom(f.properties.cluster_id);
      map.easeTo({center: f.geometry.coordinates, zoom});
    } catch (_) { /* the source was replaced under the click */ }
  });
  map.on('click', 'glmap-pins', (e) => {
    // Every lot under the click: one seller yard can hold dozens of lots at the same point, and pins
    // stop clustering past zoom 12. The popup HTML is built (and escaped) by the page that set the points.
    const seen = new Set();
    const all = (e.features || []).map((f) => f.properties.popup).filter((h) => h && !seen.has(h) && seen.add(h));
    if (!all.length) return;
    const html = all.slice(0, POPUP_MAX).map((h) => `<div class="gl-popup-item">${h}</div>`).join('')
      + (all.length > POPUP_MAX ? `<p class="gl-popup-more">+${all.length - POPUP_MAX} more at this location</p>` : '');
    new gl.Popup({maxWidth: '280px', offset: 8}).setLngLat(e.features[0].geometry.coordinates).setHTML(html).addTo(map);
  });
  for (const layer of ['glmap-clusters', 'glmap-pins']) {
    map.on('mouseenter', layer, () => { map.getCanvas().style.cursor = 'pointer'; });
    map.on('mouseleave', layer, () => { map.getCanvas().style.cursor = ''; });
  }

  return {
    maplibre: map,

    setPoints(next) {
      points = (next || []).filter((p) => p.lat != null && p.lng != null);
      map.getSource(SRC).setData({type: 'FeatureCollection', features: points.map((p) => ({
        type: 'Feature', geometry: {type: 'Point', coordinates: [Number(p.lng), Number(p.lat)]},
        properties: {popup: p.popup || '', title: p.title || ''},
      }))});
    },

    fit() {
      if (!points.length) return;
      const b = new gl.LngLatBounds();
      points.forEach((p) => b.extend([Number(p.lng), Number(p.lat)]));
      map.fitBounds(b, {padding: 40, maxZoom: 11, duration: 0});
    },

    onViewport(cb) { viewportCbs.push(cb); },
    inBounds,
    // "south,west,north,east" for the bbox param of /deals/api/lots.
    bboxParam() {
      const b = map.getBounds();
      return [b.getSouth(), b.getWest(), b.getNorth(), b.getEast()].map((v) => v.toFixed(4)).join(',');
    },
    invalidateSize() { map.resize(); },
    count() { return points.length; },
    visibleCount() { return points.filter(inBounds).length; },
  };
}
