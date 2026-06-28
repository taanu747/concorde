// Initialize Map
// originally it started in NYC but i changed it to be local so i can se the data cause ny is too far away

const map = L.map('map', { minZoom: 5 }).setView([42.485, -71.4328], 10);

// Define CartoDB maps
const mapOptions = {
    attribution: '&copy; OpenStreetMap &copy; CARTO',
    subdomains: 'abcd',
    maxZoom: 20,
    minZoom: 5,
    noWrap: true,
    zIndex: 1 // Pin base maps to the very bottom
};
const lightTiles = L.tileLayer('https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png', mapOptions);
const darkTiles = L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', mapOptions);

lightTiles.addTo(map);

// Define Live Weather Radar Layer (IEM NEXRAD Precipitation)
const nexrad = L.tileLayer('https://mesonet.agron.iastate.edu/cache/tile.py/1.0.0/nexrad-n0q-900913/{z}/{x}/{y}.png', {
    format: 'image/png',
    transparent: true,
    opacity: 0.35,
    attribution: "Weather data &copy; IEM Nexrad",
    zIndex: 10 // Force weather to always render on top of map tiles
});

// Hook up the custom Weather Mode toggle UI checkbox
document.getElementById('weather-toggle').addEventListener('change', (e) => {
    if (e.target.checked) {
        nexrad.addTo(map);
    } else {
        map.removeLayer(nexrad);
    }
});

// dark mode
let isDarkTheme = false;
document.getElementById('theme-toggle').addEventListener('change', (e) => {
    isDarkTheme = e.target.checked;
    if (isDarkTheme) {
        document.body.classList.add('dark-theme');
        map.removeLayer(lightTiles);
        darkTiles.addTo(map);
    } else {
        document.body.classList.remove('dark-theme');
        map.removeLayer(darkTiles);
        lightTiles.addTo(map);
    }

    // Force immediate redraw of existing markers so colors pop instantly
    Object.keys(aircraftMarkers).forEach(hex => {
        const marker = aircraftMarkers[hex];
        const plane = marker.planeData;
        if (plane) {
            const heading = plane.track || 0;
            const planeType = getPlaneType(plane);
            const alt = plane.alt_baro ?? plane.alt ?? plane.altitude ?? 0;
            marker.setIcon(createRotatedPlaneIcon(plane, heading, planeType, alt));

            if (aircraftPaths[hex]) {
                aircraftPaths[hex].forEach(p => {
                    if (isDarkTheme) {
                        p.setStyle({ color: '#10b981' });
                    } else {
                        if (p.trueAltColor) p.setStyle({ color: p.trueAltColor });
                    }
                });
            }
        }
    });

    if (predictionLine && selectedHex && aircraftMarkers[selectedHex]) {
        drawPrediction(aircraftMarkers[selectedHex].planeData);
    }
});

// Dictionary to keep track of active airplane markers by their HEX codes
const aircraftMarkers = {};
const aircraftPaths = {}; // Tracks the polyline trail for each plane

let selectedHex = null;
let predictionLine = null;

// Listen to popups to know which plane is selected
map.on('popupopen', async (e) => {
    const marker = e.popup._source;
    if (marker && marker.planeHex) {
        selectedHex = marker.planeHex;
        const plane = aircraftMarkers[selectedHex].planeData;
        if (plane) {
            drawPrediction(plane);

            // Asynchronously fetch Origin/Destination route data without locking map loop
            const callsign = (plane.flight || '').trim();
            if (callsign && !plane.routeFetched) {
                // Instantly lock the switch so that if the network fails/throws, we don't infinitely retry and crash the Mac's socket pool!
                plane.routeFetched = true; 
                
                // Instantly update the HTML DOM to say "Fetching..." before the API returns
                const routeDiv = document.getElementById(`route-${plane.hex}`);
                if (routeDiv) routeDiv.innerHTML = `<span class="stat-label">Route</span><span class="stat-value" style="color:#94a3b8">Fetching...</span>`;

                try {
                    const res = await fetch(`/api/route/${callsign}`);
                    const routeData = await res.json();
                    plane.routeFetched = true;

                    if (routeData.origin && routeData.destination) {
                        plane.routeStr = `${routeData.origin} ➔ ${routeData.destination}`;
                    } else {
                        plane.routeStr = 'Route Hidden';
                    }

                    // Force the popup to rerender its outer HTML strings so changes lock in
                    if (selectedHex === plane.hex) {
                        marker.getPopup().setContent(generatePopupHTML(plane));
                    }
                } catch (err) {
                    console.error("Failed to fetch route", err);
                    plane.routeStr = 'API Error';
                    if (selectedHex === plane.hex) {
                        marker.getPopup().setContent(generatePopupHTML(plane));
                    }
                }
            }
        }
    }
});

map.on('popupclose', () => {
    if (predictionLine) {
        map.removeLayer(predictionLine);
        predictionLine = null;
    }
    selectedHex = null;
});

// Flag zooming so we don't break smooth CSS plane gliding when panning/zooming
map.on('zoomstart', () => document.body.classList.add('is-zooming'));
map.on('zoomend', () => setTimeout(() => document.body.classList.remove('is-zooming'), 250));

// Major local airports for landing predictions
const LOCAL_AIRPORTS = [
    { id: 'BOS', name: 'Boston Logan', lat: 42.3656, lon: -71.0096, maxAlt: 18000, radius: 50 },
    { id: 'BED', name: 'Hanscom Field', lat: 42.4700, lon: -71.2895, maxAlt: 8000, radius: 20 },
    { id: 'MHT', name: 'Manchester-Boston', lat: 42.9326, lon: -71.4356, maxAlt: 12000, radius: 35 },
    { id: 'ORH', name: 'Worcester Regional', lat: 42.2673, lon: -71.8757, maxAlt: 10000, radius: 30 },
    { id: 'PVD', name: 'Rhode Island T. F. Green', lat: 41.7240, lon: -71.4282, maxAlt: 15000, radius: 45 }
];

const drawPrediction = (plane) => {
    if (predictionLine) map.removeLayer(predictionLine);
    if (!plane || !plane.lat || !plane.lon || plane.track === undefined || plane.gs === undefined) return;

    // Predict 10 minutes into the future
    const minutes = 10;
    const distanceNm = plane.gs * (minutes / 60);
    const trackRad = plane.track * Math.PI / 180;

    // 1 lat degree = ~60 nautical miles
    const deltaLat = (distanceNm * Math.cos(trackRad)) / 60;
    const deltaLon = (distanceNm * Math.sin(trackRad)) / (60 * Math.cos(plane.lat * Math.PI / 180));

    let targetLat = plane.lat + deltaLat;
    let targetLon = plane.lon + deltaLon;

    // Smart Landing Predictor
    const altRaw = plane.alt_baro ?? plane.alt ?? plane.altitude ?? 0;
    const alt = altRaw === 'ground' ? 0 : Number(altRaw);
    const planeType = getPlaneType(plane);

    // If plane is low & slowing down/descending (we don't care about a speed or altitude floor here)
    if (alt < 20000 && plane.gs !== undefined && plane.gs < 400) {
        for (let airport of LOCAL_AIRPORTS) {
            // Stop small planes (like Cessnas) from snapping to major commercial airports like Boston
            if (airport.id === 'BOS' && (planeType === 'small' || planeType === 'prop' || planeType === 'heli')) {
                continue; // Skip prediction for this airport
            }

            const dLat = airport.lat - plane.lat;
            const dLon = (airport.lon - plane.lon) * Math.cos(plane.lat * Math.PI / 180);
            const distNmToAirport = Math.sqrt(dLat * dLat + dLon * dLon) * 60;

            // Is it closing in on this specific airport?
            if (distNmToAirport < airport.radius && alt <= airport.maxAlt) {
                let angleToAirport = Math.atan2(dLon, dLat) * 180 / Math.PI;
                if (angleToAirport < 0) angleToAirport += 360;

                let headingDiff = Math.abs((plane.track || 0) - angleToAirport);
                if (headingDiff > 180) headingDiff = 360 - headingDiff; // shortest diff

                // Relax heading constraints dynamically as plane gets closer to the airport!
                // (e.g. they might be turning steeply onto final approach or they are already there)
                let allowedHeadingDiff = 25; // 25 degrees normally
                if (distNmToAirport < 20) allowedHeadingDiff = 90; // Wide 90-degree buffer for base turns
                if (distNmToAirport < 10) allowedHeadingDiff = 180; // Lock unconditionally when within 10 miles

                // If it's pointing its nose straight at the runway coordinates (+- allowedDiff)
                if (headingDiff < allowedHeadingDiff) {
                    targetLat = airport.lat;
                    targetLon = airport.lon;
                    break;
                }
            }
        }
    }

    predictionLine = L.polyline([
        [plane.lat, plane.lon],
        [targetLat, targetLon]
    ], {
        color: isDarkTheme ? '#10b981' : '#94a3b8', // neon green in retro mode, else gray
        weight: 2,
        dashArray: '6, 6',
        opacity: 0.8
    }).addTo(map);
};

const PLANE_PATHS = {
    'narrow': 'M256,16 C250,16 244,22 244,30 L244,140 L100,280 L100,310 L244,270 L244,400 L190,440 L190,460 L256,445 L322,460 L322,440 L268,400 L268,270 L412,310 L412,280 L268,140 L268,30 C268,22 262,16 256,16 Z',
    'wide': 'M256,6 C246,6 238,16 238,26 L238,130 L40,290 L40,320 L238,270 L238,410 L160,460 L160,480 L256,460 L352,480 L352,460 L274,410 L274,270 L472,320 L472,290 L274,130 L274,26 C274,16 266,6 256,6 Z',
    'small': 'M256,80 C250,80 244,86 244,92 L244,180 L80,180 L80,210 L244,210 L244,380 L190,400 L190,420 L256,410 L322,420 L322,400 L268,380 L268,210 L432,210 L432,180 L268,180 L268,92 C268,86 262,80 256,80 Z',
    'bizjet': 'M256,36 C250,36 246,42 246,50 L246,160 L120,300 L120,320 L246,280 L246,380 C240,380 230,385 230,395 L220,430 L180,450 L180,470 L256,450 L332,470 L332,450 L292,430 L282,395 C282,385 272,380 266,380 L266,280 L392,320 L392,300 L266,160 L266,50 C266,42 262,36 256,36 Z',
    'prop': 'M256,40 C250,40 244,46 244,52 L244,160 L60,180 L60,210 L244,220 L244,410 L170,440 L170,460 L256,445 L342,460 L342,440 L268,410 L268,220 L452,210 L452,180 L268,160 L268,52 C268,46 262,40 256,40 Z'
};

const getAltitudeColor = (alt) => {
    let a = alt;
    if (a === 'ground' || a === undefined) a = 0;
    if (a < 5000) return { hex: '#ef4444', str: '%23ef4444', strokeStr: '%237f1d1d' }; // Red
    if (a < 15000) return { hex: '#f97316', str: '%23f97316', strokeStr: '%237c2d12' }; // Orange
    if (a < 25000) return { hex: '#eab308', str: '%23eab308', strokeStr: '%23713f12' }; // Yellow
    if (a < 35000) return { hex: '#10b981', str: '%2310b981', strokeStr: '%23064e3b' }; // Green
    if (a < 45000) return { hex: '#3b82f6', str: '%233b82f6', strokeStr: '%231e3a8a' }; // Blue
    return { hex: '#a855f7', str: '%23a855f7', strokeStr: '%23581c87' }; // Purple
};

const getPlaneColor = (alt) => {
    if (isDarkTheme) {
        return { hex: '#10b981', str: '%2310b981', strokeStr: '%23064e3b' }; // Retro matrix green
    }
    return getAltitudeColor(alt);
};

// Get logical CSS class based on ADS-B category
const getPlaneType = (plane) => {
    const cat = plane.category;
    if (!cat) return 'narrow'; // default

    if (cat === 'A1') return 'small';
    if (cat === 'A3') return 'narrow';
    if (cat === 'A4' || cat === 'A5') return 'wide';
    if (cat === 'A7') return 'heli'; // helicopters

    // A2 covers small commuter twins and business jets. Differentiate by altitude/speed.
    if (cat === 'A2') {
        const alt = plane.alt_baro ?? plane.alt ?? plane.altitude ?? 0;
        const spd = plane.gs ?? plane.spd ?? plane.speed ?? 0;
        if (alt > 25000 || spd > 300) return 'bizjet';
        return 'prop';
    }

    return 'narrow';
};

// Custom DivIcon for rotating the plane image using CSS transforms
const createRotatedPlaneIcon = (plane, heading, planeType, alt) => {
    const colors = getPlaneColor(alt);
    let svgPath = '';

    if (planeType === 'heli') {
        svgPath = `%3Cpath d="M256,90 C236,90 226,110 226,140 L226,260 C226,280 240,290 246,300 L246,420 L220,440 L220,460 L292,460 L292,440 L266,420 L266,300 C272,290 286,280 286,260 L286,140 C286,110 276,90 256,90 Z" stroke-width="16" /%3E%3Cpath d="M256,40 L256,360 M96,200 L416,200" stroke-width="28" stroke-linecap="round" fill="none" /%3E`;
    } else {
        svgPath = `%3Cpath d="${PLANE_PATHS[planeType]}" stroke-width="16" /%3E`;
    }

    const svgDataUriRaw = `data:image/svg+xml;utf8,%3Csvg viewBox="0 0 512 512" xmlns="http://www.w3.org/2000/svg" fill="${colors.str}" stroke="${colors.strokeStr}" stroke-linejoin="round"%3E${svgPath}%3C/svg%3E`;
    const svgDataUri = svgDataUriRaw.replace(/"/g, '%22');

    // Evaluate if the plane is "interesting" for a glow aura
    let glowClass = '';
    const squawk = String(plane.squawk || '');
    const flight = String(plane.flight || '').toUpperCase().trim();
    const op = String(plane.operator || '').toUpperCase();

    if (['7500', '7600', '7700'].includes(squawk)) {
        glowClass = ' emergency-glow';
    } else if (flight.startsWith('RCH') || (flight.startsWith('AF') && !flight.startsWith('AFR')) || flight.startsWith('PAT') || op.includes('AIR FORCE') || op.includes('NAVY') || op.includes('ARMY') || op.includes('COAST GUARD') || op.includes('MILITARY') || op.includes('MARINE')) {
        glowClass = ' military-glow';
    } else if (alt >= 40000) {
        glowClass = ' high-alt-glow';
    }

    return L.divIcon({
        className: 'custom-plane-icon' + glowClass,
        // We use our custom upright SVGs, so 0 degrees is True North. No need to subtract 45.
        html: `<div class="plane-icon" style="transform: rotate(${heading || 0}deg); background-image: url('${svgDataUri}');"></div>`,
        iconSize: [32, 32],
        iconAnchor: [16, 16],
        popupAnchor: [0, -16]
    });
};

// Function to generate the HTML for the popup
const generatePopupHTML = (plane) => {
    const alt = plane.alt_baro !== undefined ? plane.alt_baro : (plane.alt !== undefined ? plane.alt : plane.altitude);
    const displayAlt = alt !== undefined ? (alt === 'ground' ? 'Ground' : alt.toLocaleString() + ' ft') : 'Unknown';

    const spd = plane.gs !== undefined ? plane.gs : (plane.spd !== undefined ? plane.spd : plane.speed);
    const displaySpd = spd !== undefined ? Math.round(spd) + ' kts' : 'Unknown';

    // Parse ADS-B generic category
    let typeText = plane.category || 'Unknown';
    if (plane.typecode) {
        typeText += ` (${plane.typecode})`;
    }

    // Additional identity strings
    const regStr = plane.registration ? `<div class="popup-stat"><span class="stat-label">Reg</span><span class="stat-value">${plane.registration}</span></div>` : '';
    const airlineStr = plane.operator ? `<div class="popup-stat"><span class="stat-label">Airline</span><span class="stat-value">${plane.operator}</span></div>` : '';
    const modelStr = plane.model ? `<div class="popup-stat"><span class="stat-label">Model</span><span class="stat-value">${plane.model}</span></div>` : '';

    // Support dynamic origin/destination routing
    let routeDisplay = '';
    if (plane.flight && plane.flight.trim() !== '') {
        if (plane.routeStr) {
            routeDisplay = `<div class="popup-stat" id="route-${plane.hex}"><span class="stat-label">Route</span><span class="stat-value" style="color:#eab308; font-weight:700;">${plane.routeStr}</span></div>`;
        } else {
            routeDisplay = `<div class="popup-stat" id="route-${plane.hex}"><span class="stat-label">Route</span><span class="stat-value" style="color:#94a3b8">Wait...</span></div>`;
        }
    }

    return `
        <div class="popup-container">
            <div class="popup-header">
                <span class="popup-callsign">${plane.flight ? plane.flight.trim() : 'N/A'}</span>
                <span class="popup-hex">${plane.hex}</span>
            </div>
            
            ${airlineStr}
            ${regStr}
            ${modelStr}
            ${routeDisplay}
            
            <div class="popup-stat">
                <span class="stat-label">Type</span>
                <span class="stat-value">${typeText}</span>
            </div>

            <div class="popup-stat">
                <span class="stat-label">Altitude</span>
                <span class="stat-value">${displayAlt}</span>
            </div>
            
            <div class="popup-stat">
                <span class="stat-label">Speed</span>
                <span class="stat-value">${displaySpd}</span>
            </div>
            
            <div class="popup-stat">
                <span class="stat-label">Heading</span>
                <span class="stat-value">${plane.track !== undefined ? Math.round(plane.track) + '&deg;' : 'Unknown'}</span>
            </div>
        </div>
    `;
};

// Main function to fetch data and update the map
const updateAircraft = async () => {
    try {
        const response = await fetch('/api/data');
        if (!response.ok) throw new Error('Network response was not ok');

        const data = await response.json();
        const activeHexes = new Set();

        if (data.aircraft && data.aircraft.length > 0) {
            data.aircraft.forEach(plane => {
                if (plane.lat && plane.lon) {
                    activeHexes.add(plane.hex);

                    const latLng = [plane.lat, plane.lon];
                    const heading = plane.track || 0;
                    const planeType = getPlaneType(plane);
                    const alt = plane.alt_baro ?? plane.alt ?? plane.altitude ?? 0;
                    const colorHex = getPlaneColor(alt).hex;

                    if (aircraftMarkers[plane.hex]) {
                        // Update existing marker
                        const marker = aircraftMarkers[plane.hex];

                        // Prevent the 1-second backend sync from wiping our asynchronously fetched Origin/Destination strings!
                        if (marker.planeData.routeFetched) {
                            plane.routeFetched = marker.planeData.routeFetched;
                            plane.routeStr = marker.planeData.routeStr;
                        }

                        marker.planeData = plane; // Refresh the data payload for predictions
                        marker.setLatLng(latLng);
                        marker.setIcon(createRotatedPlaneIcon(plane, heading, planeType, alt));
                        marker.getPopup().setContent(generatePopupHTML(plane));

                        // Update existing polyline path by chunking altitude bands
                        if (aircraftPaths[plane.hex]) {
                            const paths = aircraftPaths[plane.hex];
                            const currentPath = paths[paths.length - 1];
                            const latlngs = currentPath.getLatLngs();
                            const trueAltColor = getAltitudeColor(alt).hex;

                            // Only add if the coordinate actually changed
                            if (latlngs.length === 0 ||
                                latlngs[latlngs.length - 1].lat !== latLng[0] ||
                                latlngs[latlngs.length - 1].lng !== latLng[1]) {

                                if (currentPath.trueAltColor !== trueAltColor) {
                                    // Altitude bracket crossed! Spawn a brand new line segment chunk.
                                    const lastPoint = latlngs[latlngs.length - 1];
                                    const newPath = L.polyline([lastPoint, latLng], {
                                        color: isDarkTheme ? '#10b981' : trueAltColor,
                                        weight: 2,
                                        opacity: 0.8,
                                        dashArray: '4, 6'
                                    }).addTo(map);
                                    newPath.trueAltColor = trueAltColor;
                                    paths.push(newPath);
                                } else {
                                    // Simply extend the tail
                                    currentPath.addLatLng(latLng);
                                }
                            }
                        }
                    } else {
                        // Create new marker
                        const marker = L.marker(latLng, {
                            icon: createRotatedPlaneIcon(plane, heading, planeType, alt),
                            zIndexOffset: plane.alt_baro || plane.alt || plane.altitude || 0 // Higher planes appear on top
                        }).addTo(map);

                        marker.planeHex = plane.hex;
                        marker.planeData = plane;
                        marker.bindPopup(generatePopupHTML(plane));
                        aircraftMarkers[plane.hex] = marker;

                        // Create new polyline root chunk
                        const trueAltColor = getAltitudeColor(alt).hex;
                        const path = L.polyline([latLng], {
                            color: isDarkTheme ? '#10b981' : trueAltColor,
                            weight: 2,
                            opacity: 0.8,
                            dashArray: '4, 6' // dotted line
                        }).addTo(map);
                        path.trueAltColor = trueAltColor;
                        aircraftPaths[plane.hex] = [path];
                    }
                }
            });
        }

        // Remove markers that are no longer in the active data (stale planes)
        Object.keys(aircraftMarkers).forEach(hex => {
            if (!activeHexes.has(hex)) {
                map.removeLayer(aircraftMarkers[hex]);
                delete aircraftMarkers[hex];

                // Also remove the path array
                if (aircraftPaths[hex]) {
                    aircraftPaths[hex].forEach(p => map.removeLayer(p));
                    delete aircraftPaths[hex];
                }
            }
        });

        // Refresh the prediction line if the user has a popup open
        if (selectedHex && aircraftMarkers[selectedHex]) {
            drawPrediction(aircraftMarkers[selectedHex].planeData);
        } else if (selectedHex && !aircraftMarkers[selectedHex]) {
            if (predictionLine) map.removeLayer(predictionLine);
            predictionLine = null;
            selectedHex = null;
        }

        // Update the aircraft counter UI
        const counterUI = document.getElementById('aircraft-count');
        if (counterUI) {
            counterUI.textContent = `${activeHexes.size} Aircraft`;
        }

        document.querySelector('.dot').className = 'dot live';

    } catch (error) {
        console.error('Error fetching aircraft data:', error);
        document.querySelector('.dot').className = 'dot offline';
    }
};

// Initial fetch and set interval for polling every 1 second
updateAircraft();
setInterval(updateAircraft, 1000);

// --- History & Search Feature ---
const searchBar = document.getElementById('search-bar');
const searchResults = document.getElementById('search-results');
let searchTimeout;
let historicalPathLayer = null;
let historicalMarker = null;

if (searchBar && searchResults) {
    searchBar.addEventListener('input', (e) => {
        clearTimeout(searchTimeout);
        const query = e.target.value.trim();
        
        if (query.length < 2) {
            searchResults.style.display = 'none';
            return;
        }
        
        searchTimeout = setTimeout(async () => {
            try {
                const res = await fetch(`/api/search?query=${query}`);
                const data = await res.json();
                
                if (data.length === 0) {
                    searchResults.innerHTML = '<div class="search-result-item"><div class="search-result-details">No aircraft found</div></div>';
                    searchResults.style.display = 'block';
                    return;
                }
                
                searchResults.innerHTML = data.map(plane => `
                    <div class="search-result-item" data-hex="${plane.hex}">
                        <div class="search-result-callsign">${plane.callsign || 'Unknown'} <span style="font-size: 0.7em; color: #94a3b8;">(${plane.hex})</span></div>
                        <div class="search-result-details">Last seen: ${plane.last_seen} UTC</div>
                        <div class="search-result-details">Alt: ${Math.round(plane.altitude || 0)} ft</div>
                    </div>
                `).join('');
                
                searchResults.style.display = 'block';
                
                // Add click listeners to items
                document.querySelectorAll('.search-result-item[data-hex]').forEach(item => {
                    item.addEventListener('click', () => {
                        const hex = item.getAttribute('data-hex');
                        loadAircraftHistory(hex);
                        searchResults.style.display = 'none';
                        searchBar.value = '';
                    });
                });
                
            } catch (error) {
                console.error('Search failed:', error);
            }
        }, 300);
    });

    // Close results when clicking outside
    document.addEventListener('click', (e) => {
        if (!e.target.closest('#search-container')) {
            searchResults.style.display = 'none';
        }
    });
}

async function loadAircraftHistory(hex) {
    try {
        const res = await fetch(`/api/history?hex=${hex}`);
        const data = await res.json();
        
        if (data.length === 0) return;
        
        if (historicalPathLayer) {
            map.removeLayer(historicalPathLayer);
            historicalPathLayer = null;
        }
        if (historicalMarker) {
            map.removeLayer(historicalMarker);
            historicalMarker = null;
        }
        
        // Extract latlngs (ensure lat/lon are not null)
        const latlngs = data.filter(pt => pt.lat !== null && pt.lon !== null).map(pt => [pt.lat, pt.lon]);
        
        if (latlngs.length > 0) {
            historicalPathLayer = L.polyline(latlngs, {
                color: '#eab308', // Yellow
                weight: 3,
                opacity: 0.9,
                dashArray: null // Solid line for history
            }).addTo(map);
            
            // Pan to the most recent position
            const latestPos = latlngs[latlngs.length - 1];
            map.flyTo(latestPos, 11, { duration: 1.5 });
            
            // If we have the live marker, open its popup
            if (aircraftMarkers[hex]) {
                aircraftMarkers[hex].openPopup();
            } else {
                // Extract exact heading from the last known data point, or fallback to calculating it
                let heading = 0;
                const validPoints = data.filter(pt => pt.lat !== null && pt.lon !== null);
                if (validPoints.length > 0) {
                    const lastPt = validPoints[validPoints.length - 1];
                    if (lastPt.heading !== null && lastPt.heading !== undefined) {
                        heading = lastPt.heading;
                    } else if (validPoints.length >= 2) {
                        // Fallback to GPS coordinate math for older database entries
                        let p1 = null;
                        const p2 = lastPt;
                        // Iterate backwards to find a distinct point
                        for (let i = validPoints.length - 2; i >= 0; i--) {
                            if (validPoints[i].lat !== p2.lat || validPoints[i].lon !== p2.lon) {
                                p1 = validPoints[i];
                                break;
                            }
                        }
                        
                        if (p1) {
                            const dLon = (p2.lon - p1.lon) * Math.cos(p1.lat * Math.PI / 180);
                            const dLat = p2.lat - p1.lat;
                            heading = Math.atan2(dLon, dLat) * 180 / Math.PI;
                            heading = (heading + 360) % 360;
                        }
                    }
                }

                // Add a temporary gray marker for the last known position
                const svgPath = PLANE_PATHS['narrow']; 
                const svgDataUriRaw = `data:image/svg+xml;utf8,%3Csvg viewBox="0 0 512 512" xmlns="http://www.w3.org/2000/svg" fill="%2364748b" stroke="%23334155" stroke-linejoin="round"%3E%3Cpath d="${svgPath}" stroke-width="16" /%3E%3C/svg%3E`;
                const svgDataUri = svgDataUriRaw.replace(/"/g, '%22');
                
                const offlineIcon = L.divIcon({
                    className: 'custom-plane-icon',
                    html: `<div class="plane-icon" style="transform: rotate(${heading}deg); background-image: url('${svgDataUri}'); opacity: 0.8;"></div>`,
                    iconSize: [32, 32],
                    iconAnchor: [16, 16],
                    popupAnchor: [0, -16]
                });

                historicalMarker = L.marker(latestPos, { icon: offlineIcon, zIndexOffset: 1000 }).addTo(map);
                
                historicalMarker.bindPopup(`<div style="color: #94a3b8; font-family: 'Inter', sans-serif; padding: 5px; text-align: center;"><b>${hex}</b><br><span style="font-size: 0.85em;">Offline. Last known location.</span></div>`).openPopup();
            }
        }
    } catch (error) {
        console.error('Failed to load history:', error);
    }
}

// Clear historical path when clicking on the map (off a plane)
map.on('click', () => {
    if (historicalPathLayer) {
        map.removeLayer(historicalPathLayer);
        historicalPathLayer = null;
    }
    if (historicalMarker) {
        map.removeLayer(historicalMarker);
        historicalMarker = null;
    }
});

// --- Analytics: Heatmap ---
let heatmapLayer = null;

document.getElementById('heatmap-toggle').addEventListener('change', async (e) => {
    if (e.target.checked) {
        try {
            const res = await fetch('/api/analytics/heatmap');
            const data = await res.json();
            
            // Format for Leaflet.heat: [lat, lon, intensity]
            // Multiplier reduced to make it less sensitive
            const heatData = data.map(pt => [pt[0], pt[1], pt[2] * 0.2]);
            
            heatmapLayer = L.heatLayer(heatData, {
                radius: 20,
                blur: 30,
                max: 3, // Higher max means more overlapping planes required to turn red
                maxZoom: 14,
                gradient: { 0.4: 'blue', 0.6: 'cyan', 0.7: 'lime', 0.8: 'yellow', 1.0: 'red' }
            }).addTo(map);
        } catch (err) {
            console.error("Failed to load heatmap", err);
        }
    } else {
        if (heatmapLayer) {
            map.removeLayer(heatmapLayer);
            heatmapLayer = null;
        }
    }
});

// --- Analytics: Weather Deviations ---
let deviationsLayerGroup = L.layerGroup().addTo(map);

document.getElementById('deviations-toggle').addEventListener('change', async (e) => {
    if (e.target.checked) {
        try {
            const res = await fetch('/api/analytics/weather-deviations');
            const data = await res.json();
            
            data.forEach(dev => {
                let reason = dev.hc > 15 ? `Heading change of ${Math.round(dev.hc)}&deg;` : `Altitude change of ${Math.round(dev.ac)} ft`;
                
                const marker = L.circleMarker([dev.lat, dev.lon], {
                    radius: 8,
                    fillColor: "#ef4444",
                    color: "#000",
                    weight: 1,
                    opacity: 1,
                    fillOpacity: 0.8
                });
                
                marker.bindPopup(`
                    <div style="font-family:'Inter',sans-serif;">
                        <strong style="color:#ef4444;">Deviation Detected</strong><br>
                        Flight: ${dev.callsign || dev.hex}<br>
                        Reason: ${reason}<br>
                        <button onclick="loadAircraftHistory('${dev.hex}')" style="margin-top:5px; padding:3px 8px; background:#10b981; color:#fff; border:none; border-radius:4px; cursor:pointer;">View Path</button>
                    </div>
                `);
                
                deviationsLayerGroup.addLayer(marker);
            });
        } catch (err) {
            console.error("Failed to load deviations", err);
        }
    } else {
        deviationsLayerGroup.clearLayers();
    }
});
