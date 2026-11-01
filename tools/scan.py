"""粗扫：按固定 fps 抽样跑姿态估计，输出 data/<tag>_coarse.jsonl（可断点续跑）

用法: python scan.py <视频> <tag> [fps]
"""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bbkit import (iter_frames, Poser, pick_person, kp_norm_to_px, read_jsonl, probe)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")


def main(video, tag, fps=2.5):
    os.makedirs(DATA, exist_ok=True)
    out_path = os.path.join(DATA, f"{tag}_coarse.jsonl")
    info = probe(video)
    prev = read_jsonl(out_path)
    start = 0.0
    if prev:
        start = prev[-1]["t"]
        print(f"resume from t={start:.1f}s ({len(prev)} frames already done)", flush=True)
    print(f"video {os.path.basename(video)} {info['size']} {info['fps']}fps {info['duration']}s", flush=True)
    step = 1.0 / fps
    poser = Poser()
    next_t = start + step if prev else 0.0
    n = 0
    prev_cx = None
    f = open(out_path, "a")
    t0 = time.time()
    for t, sb in iter_frames(video, t_start=next_t):
        if t < next_t:
            continue
        next_t = t + step
        dets = poser.detect_sb(sb)
        W, H = 1920, 1080
        # 统一约定：kp 的 y 已翻转为「原点左上」的归一化值，像素 = [x*1920, y*1080]
        ppl = [kp_norm_to_px(d, 1.0, 1.0) for d in dets]
        d = pick_person(ppl, prev_cx)
        if d is None:
            prev_cx = None
            rec = {"t": round(t, 3), "n": len(ppl), "kp": None}
        else:
            xs = [v[0] for v in d.values()]
            prev_cx = (max(xs) + min(xs)) / 2
            rec = {"t": round(t, 3), "n": len(ppl), "W": 1920, "H": 1080,
                   "kp": {k: [round(v[0], 5), round(v[1], 5), round(v[2], 3)] for k, v in d.items()}}
        f.write(json.dumps(rec) + "\n")
        f.flush()
        n += 1
        if n % 100 == 0:
            el = time.time() - t0
            print(f"  {t:6.1f}s / {info['duration']:.0f}s  rate={n/el:.1f}fps  eta={(info['duration']-t)/(n/el):.0f}s", flush=True)
    f.close()
    print(f"DONE {out_path} new={n} elapsed={time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    v = sys.argv[1]
    tg = sys.argv[2]
    fp = float(sys.argv[3]) if len(sys.argv) > 3 else 2.5
    main(v, tg, fp)
