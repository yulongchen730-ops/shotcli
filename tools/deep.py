"""对指定时间点做逐帧深扫（更平滑的序列，用于报告播放器 / 慢放视频）

用法: python deep.py <视频> <tag> <fps> <t1,t2,...> [pre] [post] [suf]

suf 是输出后缀：留空写 deep_<t>.json（分析/报告用，30fps）；
传 "60" 写 deep60_<t>.json（出慢放视频用，60fps）—— 两套互不覆盖。
"""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shots import load_sig, fine_shot, detect_events

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
FRAMES = os.path.join(ROOT, "frames")


def main(video, tag, fps, times, pre=0.95, post=2.6, target_h=760, suf=""):
    sig = load_sig(os.path.join(DATA, f"{tag}_coarse.jsonl"))
    evs = json.load(open(os.path.join(DATA, f"{tag}_events.json")))
    out_dir = os.path.join(DATA, tag)
    frame_dir = os.path.join(FRAMES, tag)
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(frame_dir, exist_ok=True)
    for t in times:
        ev = min(evs, key=lambda e: abs(e["t"] - t))
        fp = os.path.join(out_dir, f"deep{suf}_{ev['t']:.1f}.json")
        if os.path.exists(fp):
            print("skip", fp, flush=True)
            continue
        t0 = time.time()
        meta, seq = fine_shot(video, sig, ev, f"{tag}D{ev['t']:.0f}{suf}", out_dir, frame_dir,
                              fps=fps, pre=pre, post=post, target_h=target_h)
        json.dump({"meta": meta, "seq": seq}, open(fp, "w"))
        print(f"deep t={ev['t']:.1f}s frames={len(seq)} {time.time()-t0:.0f}s -> {fp}", flush=True)
    print("DONE", flush=True)


if __name__ == "__main__":
    v, tg, fp = sys.argv[1], sys.argv[2], float(sys.argv[3])
    ts = [float(x) for x in sys.argv[4].split(",")]
    pre = float(sys.argv[5]) if len(sys.argv) > 5 else 0.95
    post = float(sys.argv[6]) if len(sys.argv) > 6 else 2.6
    suf = sys.argv[7] if len(sys.argv) > 7 else ""
    main(v, tg, fp, ts, pre, post, 760, suf)
