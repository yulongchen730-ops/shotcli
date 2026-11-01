"""篮球检测（HSV 橙色 + 圆形连通域）；用于：验证球在画面内、用球径标定尺度、跟踪飞行轨迹"""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import Quartz
import numpy as np
from bbkit import make_generator, frame_at, save_jpeg, crop_cg, scale_cg

BALL_DIAM_CM = 24.0   # 7 号球直径约 24.0–24.6cm


def cg_to_np(cg):
    W, H = Quartz.CGImageGetWidth(cg), Quartz.CGImageGetHeight(cg)
    cs = Quartz.CGColorSpaceCreateDeviceRGB()
    ctx = Quartz.CGBitmapContextCreate(None, W, H, 8, W * 4, cs,
                                       Quartz.kCGImageAlphaPremultipliedFirst | Quartz.kCGBitmapByteOrder32Little)
    Quartz.CGContextDrawImage(ctx, Quartz.CGRectMake(0, 0, W, H), cg)
    data = Quartz.CGBitmapContextGetData(ctx)
    buf = data.as_buffer(W * H * 4) if hasattr(data, "as_buffer") else bytes(data)
    return np.frombuffer(buf, dtype=np.uint8).reshape(H, W, 4)[:, :, [2, 1, 0]].copy()  # -> RGB


def orange_mask(rgb):
    r = rgb[:, :, 0].astype(np.int16)
    g = rgb[:, :, 1].astype(np.int16)
    b = rgb[:, :, 2].astype(np.int16)
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1), 0)
    return (r - b > 45) & (r - g > 18) & (r > 95) & (sat > 0.42) & (g - b > -8)


def label_components(mask, min_px=12):
    """粗网格上的连通域（快），返回 [(x0,y0,x1,y1,npx)]（像素坐标）"""
    H, W = mask.shape
    out = []
    seen = np.zeros_like(mask, dtype=bool)
    ys, xs = np.nonzero(mask)
    for k in range(len(ys)):
        y0, x0 = ys[k], xs[k]
        if seen[y0, x0]:
            continue
        stack = [(y0, x0)]
        seen[y0, x0] = True
        pts = []
        while stack:
            y, x = stack.pop()
            pts.append((y, x))
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    ny, nx = y + dy, x + dx
                    if 0 <= ny < H and 0 <= nx < W and mask[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        stack.append((ny, nx))
        if len(pts) < min_px:
            continue
        pa = np.array(pts)
        out.append((int(pa[:, 1].min()), int(pa[:, 0].min()), int(pa[:, 1].max()), int(pa[:, 0].max()), len(pts)))
    return out


def detect_balls(cg, ds=4, roi=None, min_d=14, max_d=110, min_fill=0.45):
    """返回 [(cx, cy, diam_px, fill, npx)]（原图坐标）"""
    full = cg_to_np(cg)
    H, W = full.shape[:2]
    sub = full
    ox, oy = 0, 0
    if roi:
        x0, y0, x1, y1 = roi
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(W, x1), min(H, y1)
        sub = full[y0:y1, x0:x1]
        ox, oy = x0, y0
    small = sub[::ds, ::ds]
    m = orange_mask(small)
    comps = label_components(m, min_px=max(4, (min_d // ds) ** 2 // 3))
    res = []
    for x0, y0, x1, y1, n in comps:
        w = (x1 - x0 + 1) * ds
        h = (y1 - y0 + 1) * ds
        if not (min_d <= (w + h) / 2 <= max_d):
            continue
        if max(w, h) > 2.2 * min(w, h):
            continue
        # 在原图 ROI 上精修
        sx0, sy0 = max(0, x0 * ds - 4), max(0, y0 * ds - 4)
        sx1, sy1 = min(sub.shape[1], (x1 + 1) * ds + 4), min(sub.shape[0], (y1 + 1) * ds + 4)
        mm = orange_mask(sub[sy0:sy1, sx0:sx1])
        if mm.sum() < 20:
            continue
        yy, xx = np.nonzero(mm)
        cx = xx.mean() + sx0 + ox
        cy = yy.mean() + sy0 + oy
        area = int(mm.sum())
        diam = 2.0 * np.sqrt(area / np.pi)
        fill = area / max(1.0, (xx.max() - xx.min() + 1) * (yy.max() - yy.min() + 1))
        if fill < min_fill:
            continue
        res.append((round(float(cx), 1), round(float(cy), 1), round(float(diam), 1), round(float(fill), 2), area))
    res.sort(key=lambda t: -t[4])
    return res


if __name__ == "__main__":
    video = sys.argv[1]
    ts = [float(x) for x in sys.argv[2].split(",")]
    crop = None
    if "--crop" in sys.argv:
        crop = [int(v) for v in sys.argv[sys.argv.index("--crop") + 1].split(",")]
    outdir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "ballqa")
    os.makedirs(outdir, exist_ok=True)
    gen, _ = make_generator(video, (1920, 1080))
    for t in ts:
        cg = frame_at(gen, t)
        if cg is None:
            print("fail", t)
            continue
        if crop:
            cg = crop_cg(cg, Quartz.CGRectMake(*crop))
        t0 = time.time()
        bl = detect_balls(cg)
        print(f"t={t:8.2f} {time.time()-t0:.2f}s blobs={len(bl)}: " +
              " | ".join(f"({c[0]:.0f},{c[1]:.0f}) d={c[2]:.0f}px fill={c[3]}" for c in bl[:5]), flush=True)
        save_jpeg(cg, os.path.join(outdir, f"bqa_{t:.2f}.jpg"), 0.85)
