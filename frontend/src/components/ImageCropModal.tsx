import { X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

type CropState = {
  zoom: number; // 1..max
  offsetX: number; // px in viewport
  offsetY: number; // px in viewport
};

function clamp(n: number, a: number, b: number) {
  return Math.min(b, Math.max(a, n));
}

function readFileAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result || ""));
    r.onerror = () => reject(new Error("read failed"));
    r.readAsDataURL(file);
  });
}

function loadImage(src: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error("image load failed"));
    img.src = src;
  });
}

export default function ImageCropModal(props: {
  open: boolean;
  file: File | null;
  title: string;
  aspect: number; // width / height
  outWidth: number;
  outHeight: number;
  outMime: "image/png" | "image/jpeg";
  outQuality?: number; // jpeg only
  onCancel: () => void;
  onConfirm: (dataUrl: string) => void;
}) {
  const [dataUrl, setDataUrl] = useState<string>("");
  const [img, setImg] = useState<HTMLImageElement | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [crop, setCrop] = useState<CropState>({ zoom: 1, offsetX: 0, offsetY: 0 });

  const viewport = useMemo(() => {
    // Keep the UI compact while preserving aspect.
    const maxW = 420;
    const w = maxW;
    const h = Math.max(96, Math.round(w / props.aspect));
    return { w, h };
  }, [props.aspect]);

  const draggingRef = useRef<{ x: number; y: number } | null>(null);

  useEffect(() => {
    if (!props.open) return;
    setError(null);
    setCrop({ zoom: 1, offsetX: 0, offsetY: 0 });
  }, [props.open]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (!props.open || !props.file) return;
      try {
        const url = await readFileAsDataUrl(props.file);
        if (cancelled) return;
        setDataUrl(url);
        const image = await loadImage(url);
        if (cancelled) return;
        setImg(image);
      } catch (e) {
        if (cancelled) return;
        setError(String((e as Error)?.message || e));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [props.open, props.file]);

  const baseScale = useMemo(() => {
    if (!img) return 1;
    return Math.max(viewport.w / img.naturalWidth, viewport.h / img.naturalHeight);
  }, [img, viewport.h, viewport.w]);

  const maxZoom = 4;
  const effectiveScale = baseScale * crop.zoom;

  function clampOffsets(next: CropState): CropState {
    if (!img) return next;
    const nextScale = baseScale * next.zoom;
    const halfImgW = (img.naturalWidth * nextScale) / 2;
    const halfImgH = (img.naturalHeight * nextScale) / 2;
    const halfVw = viewport.w / 2;
    const halfVh = viewport.h / 2;
    const maxX = Math.max(0, halfImgW - halfVw);
    const maxY = Math.max(0, halfImgH - halfVh);
    return {
      ...next,
      offsetX: clamp(next.offsetX, -maxX, maxX),
      offsetY: clamp(next.offsetY, -maxY, maxY)
    };
  }

  useEffect(() => {
    setCrop((prev) => clampOffsets(prev));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [img, viewport.w, viewport.h, crop.zoom]);

  function toDataUrlCropped(): string {
    if (!img) throw new Error("no image loaded");
    const canvas = document.createElement("canvas");
    canvas.width = props.outWidth;
    canvas.height = props.outHeight;
    const ctx = canvas.getContext("2d");
    if (!ctx) throw new Error("no canvas context");

    const cx = viewport.w / 2;
    const cy = viewport.h / 2;

    // Compute source rect that maps to viewport.
    const sx0 = (0 - cx - crop.offsetX) / effectiveScale + img.naturalWidth / 2;
    const sy0 = (0 - cy - crop.offsetY) / effectiveScale + img.naturalHeight / 2;
    const sx1 = (viewport.w - cx - crop.offsetX) / effectiveScale + img.naturalWidth / 2;
    const sy1 = (viewport.h - cy - crop.offsetY) / effectiveScale + img.naturalHeight / 2;

    const sw = sx1 - sx0;
    const sh = sy1 - sy0;

    // Draw; browser will handle fractional coords.
    ctx.drawImage(img, sx0, sy0, sw, sh, 0, 0, props.outWidth, props.outHeight);

    if (props.outMime === "image/jpeg") return canvas.toDataURL("image/jpeg", props.outQuality ?? 0.92);
    return canvas.toDataURL("image/png");
  }

  if (!props.open) return null;

  return (
    <div className="cropOverlay" role="presentation" onClick={props.onCancel}>
      <div
        className="cropModal"
        role="dialog"
        aria-label={props.title}
        onClick={(e) => {
          e.stopPropagation();
        }}
      >
        <div className="cropHeader">
          <div className="cropTitle">{props.title}</div>
          <button className="iconBtn iconOnly" onClick={props.onCancel} aria-label="关闭" title="关闭">
            <X size={16} />
          </button>
        </div>

        <div className="cropBody">
          {error ? <div className="error">{error}</div> : null}
          <div
            className="cropViewport"
            style={{ width: viewport.w, height: viewport.h }}
            onPointerDown={(e) => {
              (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
              draggingRef.current = { x: e.clientX, y: e.clientY };
            }}
            onPointerUp={() => {
              draggingRef.current = null;
            }}
            onPointerCancel={() => {
              draggingRef.current = null;
            }}
            onPointerMove={(e) => {
              if (!draggingRef.current) return;
              const dx = e.clientX - draggingRef.current.x;
              const dy = e.clientY - draggingRef.current.y;
              draggingRef.current = { x: e.clientX, y: e.clientY };
              setCrop((prev) => clampOffsets({ ...prev, offsetX: prev.offsetX + dx, offsetY: prev.offsetY + dy }));
            }}
          >
            {dataUrl ? (
              (() => {
                const w = img ? img.naturalWidth * effectiveScale : 0;
                const h = img ? img.naturalHeight * effectiveScale : 0;
                const x = (viewport.w - w) / 2 + crop.offsetX;
                const y = (viewport.h - h) / 2 + crop.offsetY;
                return (
                  <div
                    className="cropImage"
                    style={{
                      backgroundImage: `url(${dataUrl})`,
                      backgroundRepeat: "no-repeat",
                      backgroundPosition: `${x}px ${y}px`,
                      backgroundSize: `${w}px ${h}px`
                    }}
                  />
                );
              })()
            ) : (
              <div className="cropImage placeholder" />
            )}
          </div>

          <label className="cropRow">
            <span className="cropLabel">缩放</span>
            <input
              className="cropRange"
              type="range"
              min={1}
              max={maxZoom}
              step={0.01}
              value={crop.zoom}
              onChange={(e) => setCrop((prev) => clampOffsets({ ...prev, zoom: parseFloat(e.target.value) }))}
            />
          </label>

          <div className="cropHint">拖动图片调整裁剪区域；默认会取图片中央最大区域。</div>
        </div>

        <div className="cropFooter">
          <button onClick={props.onCancel}>取消</button>
          <button
            className="primary"
            disabled={!img}
            onClick={() => {
              try {
                const out = toDataUrlCropped();
                props.onConfirm(out);
              } catch (e) {
                setError(String((e as Error)?.message || e));
              }
            }}
          >
            确认裁剪
          </button>
        </div>
      </div>
    </div>
  );
}
