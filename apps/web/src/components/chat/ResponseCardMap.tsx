import { Map as PigeonMap, Overlay } from "pigeon-maps";
import { useLayoutEffect, useMemo, useRef, useState } from "react";
import { mapView, type MapPlace } from "../../lib/responseCards";

const HEIGHT = 200;
const tileUrl = (x: number, y: number, z: number) => `https://tile.openstreetmap.org/${z}/${x}/${y}.png`;

type NumberedPlace = MapPlace & { number?: number };

/** The map on a places card: rough positions on OpenStreetMap, without any account or key. */
export default function ResponseCardMap({ places }: { places: NumberedPlace[] }) {
  const ref = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  useLayoutEffect(() => {
    const element = ref.current;
    if (!element) return undefined;
    setWidth(element.clientWidth);
    if (typeof ResizeObserver === "undefined") return undefined;
    const observer = new ResizeObserver(() => setWidth(element.clientWidth));
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  const located = useMemo(() => places.flatMap((place) => place.lat !== undefined && place.lng !== undefined && place.number !== undefined
    ? [{ lat: place.lat, lng: place.lng, number: place.number, name: place.name }]
    : []), [places]);
  const view = useMemo(() => width ? mapView(located, width, HEIGHT) : null, [located, width]);
  return <div ref={ref} className="response-card__map" style={{ height: HEIGHT }}>
    {view ? <PigeonMap
      width={width}
      height={HEIGHT}
      defaultCenter={view.center}
      defaultZoom={view.zoom}
      provider={tileUrl}
      metaWheelZoom
      twoFingerDrag
      attributionPrefix={false}
      attribution={<a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">© OpenStreetMap contributors</a>}
    >
      {located.map((place) => <Overlay key={place.number} anchor={[place.lat, place.lng]} offset={[11, 11]}>
        <span className="response-card__pin" role="img" aria-label={place.name}>{place.number}</span>
      </Overlay>)}
    </PigeonMap> : null}
  </div>;
}
