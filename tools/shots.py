"""投篮事件检测 + 精细分析（裁剪放大后逐帧姿态）

阶段1: 从粗扫数据里找"手举过肩"事件（投篮动作的必要条件）
阶段2: 对每个事件窗口用 20fps 逐帧姿态（裁剪到球员区域并放大，精度更高）
输出: data/<tag>/shot_*.json （每个投篮一个文件，天然可断点续跑）+ frames/<tag>/*.jpg

用法: python shots.py <视频> <tag> [最多分析几个球]
"""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bbkit import (make_generator, frame_at, crop_cg, scale_cg, save_jpeg, Poser,
                   read_jsonl, kp_norm_to_px, frame_features, joint, hr, SHOULDER, ELBOW,
                   WRIST, HIP, KNEE, ANKLE, SIDES)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
FRAMES = os.path.join(ROOT, "frames")
W0, H0 = 1920, 1080


# ------------------------------------------------------------------ 事件检测

def load_sig(path):
    """读取粗扫数据，统一转成 1920x1080 像素坐标（原点左上）"""
    recs = read_jsonl(path)
    out = []
    for r in recs:
        if not r.get("kp"):
            out.append({"t": r["t"], "kp": None})
            continue
        W = r.get("W") or 0
        H = r.get("H") or 0
        W = W if W > 1 else 1920
        H = H if H > 1 else 1080
        kp = {k: [v[0] * W, v[1] * H, v[2]] for k, v in r["kp"].items()}
        out.append({"t": r["t"], "kp": kp})
    return out


def detect_events(sig, exist=0.03, min_peak=0.08, min_gap=2.0):
    raw = []
    for side in SIDES:
        run = None
        for r in sig:
            v = None
            if r["kp"]:
                f = frame_features({"t": r["t"], "i": 0, "kp": r["kp"]})
                v = f[side]["wr_over_sh"] if f else None
            if v is not None and v > exist:
                if run is None:
                    run = {"start": r["t"], "end": r["t"], "peak": v}
                else:
                    run["end"] = r["t"]
                    run["peak"] = max(run["peak"], v)
            else:
                if run:
                    raw.append(dict(run, hand=side, t=(run["start"] + run["end"]) / 2))
                    run = None
        if run:
            raw.append(dict(run, hand=side, t=(run["start"] + run["end"]) / 2))
    ev = [e for e in raw if e["peak"] >= min_peak]
    ev.sort(key=lambda e: e["start"])
    merged = []
    for e in ev:
        if merged and e["start"] - merged[-1]["end"] < min_gap:
            m = merged[-1]
            if e["peak"] > m["peak"]:
                m.update({"peak": e["peak"], "t": e["t"], "hand": e["hand"]})
            m["end"] = max(m["end"], e["end"])
        else:
            merged.append(dict(e))
    for m in merged:
        m["dur"] = round(m["end"] - m["start"], 2)
        for k in ("start", "end", "t"):
            m[k] = round(m[k], 2)
        m["peak"] = round(m["peak"], 3)
    return merged


def coarse_bbox(sig, t):
    """t 附近粗扫帧的关节包围盒（像素）"""
    best = None
    for r in sig:
        if not r["kp"]:
            continue
        if best is None or abs(r["t"] - t) < abs(best["t"] - t):
            best = r
    if best is None:
        return None
    xs = [v[0] for v in best["kp"].values()]
    ys = [v[1] for v in best["kp"].values()]
    return [min(xs), min(ys), max(xs), max(ys)]


def crop_box(bbox, margin=0.6, min_side=460, ar=0.82):
    x0, y0, x1, y1 = bbox
    w = max((x1 - x0) * (1 + 2 * margin), min_side)
    h = max((y1 - y0) * (1 + 2 * margin), min_side)
    if w / h > ar:
        w = h * ar
    else:
        h = w / ar
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    x0 = max(0, min(cx - w / 2, W0 - w))
    y0 = max(0, min(cy - h / 2, H0 - h))
    return [round(x0), round(y0), round(min(w, W0)), round(min(h, H0))]


# ------------------------------------------------------------------ 精细分析

def fine_shot(video, sig, ev, tag, out_dir, frame_dir, fps=15.0, pre=0.8, post=0.5, target_h=900):
    box = crop_box(coarse_bbox(sig, ev["t"]) or [700, 300, 1000, 800])
    gen, _ = make_generator(video, (W0, H0))
    poser = Poser()
    seq = []
    n = int((pre + post) * fps)
    cw = int(box[2] * target_h / box[3])
    ch = target_h
    miss = 0
    for i in range(n):
        t = ev["t"] - pre + i / fps
        cg = frame_at(gen, t)
        if cg is None:
            miss += 1
            continue
        c = crop_cg(cg, __import__("Quartz").CGRectMake(box[0], box[1], box[2], box[3]))
        if c is None:
            miss += 1
            continue
        sc = scale_cg(c, cw, ch)
        kp = None
        dets = poser.detect_cg(sc)
        if dets:
            def area(d):
                xs = [v[0] for v in d.values()]
                ys = [v[1] for v in d.values()]
                return (max(xs) - min(xs)) * (max(ys) - min(ys))
            d = max(dets, key=area)
            kp = kp_norm_to_px(d, cw, ch)
        fn = f"{tag}_{ev['t']:.1f}s_{i:02d}.jpg".replace(" ", "")
        save_jpeg(sc, os.path.join(frame_dir, fn), 0.8)
        seq.append({"t": round(t, 3), "i": i, "kp": kp, "img": fn, "cw": cw, "ch": ch})
        if i % 5 == 0:
            print(f"      frame {i}/{n} t={t:.2f}s kp={'ok' if kp else 'None'}", flush=True)
    if not seq:
        raise RuntimeError(
            f"未取到任何帧 t_center={ev['t']:.2f}s miss={miss}/{n} —— "
            "多半是硬件解码器被其它进程占用（比如同时有后台扫描在跑），"
            "等占用方结束再重试"
        )
    meta = {"video": video, "tag": tag, "t_center": ev["t"], "hand": ev["hand"],
            "peak_coarse": ev["peak"], "box": box, "crop_w": cw, "crop_h": ch,
            "fps": fps, "pre": pre, "post": post, "event": ev}
    return meta, seq


def main(video, tag, limit=14):
    os.makedirs(DATA, exist_ok=True)
    out_dir = os.path.join(DATA, tag)
    frame_dir = os.path.join(FRAMES, tag)
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(frame_dir, exist_ok=True)
    sig = load_sig(os.path.join(DATA, f"{tag}_coarse.jsonl"))
    print(f"coarse frames={len(sig)} span={sig[0]['t']:.0f}-{sig[-1]['t']:.0f}s", flush=True)
    evs = detect_events(sig)
    print(f"events={len(evs)}", flush=True)
    for e in evs:
        print(f"   t={e['t']:8.2f} hand={e['hand']:5s} peak={e['peak']:.2f} dur={e['dur']:.1f}", flush=True)
    json.dump(evs, open(os.path.join(DATA, f"{tag}_events.json"), "w"), ensure_ascii=False, indent=1)
    # 优先分析峰值高的
    order = sorted(evs, key=lambda e: -e["peak"])[:limit]
    order.sort(key=lambda e: e["t"])
    for idx, ev in enumerate(order):
        fp = os.path.join(out_dir, f"shot_{idx:02d}.json")
        if os.path.exists(fp):
            print(f"skip {idx} (done)", flush=True)
            continue
        t0 = time.time()
        meta, seq = fine_shot(video, sig, ev, f"{tag}{idx:02d}", out_dir, frame_dir)
        json.dump({"meta": meta, "seq": seq}, open(fp, "w"))
        print(f"shot {idx} t={ev['t']:.1f}s frames={len(seq)} {time.time()-t0:.0f}s", flush=True)
    print("ALL DONE", flush=True)


if __name__ == "__main__":
    v, tg = sys.argv[1], sys.argv[2]
    lim = int(sys.argv[3]) if len(sys.argv) > 3 else 14
    main(v, tg, lim)
