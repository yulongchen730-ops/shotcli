"""按事件列表批量精细分析（按 t 去重，可断点续跑）

用法: python run_shots.py <视频> <tag> <最多几个>
已有的 data/<tag>/shot_NN.json 里如果已经有相同时间的球，就跳过。
"""
import sys, os, json, time, glob
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shots import load_sig, fine_shot, detect_events

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
FRAMES = os.path.join(ROOT, "frames")


def main(video, tag, limit=20):
    sig = load_sig(os.path.join(DATA, f"{tag}_coarse.jsonl"))
    evs = detect_events(sig)
    json.dump(evs, open(os.path.join(DATA, f"{tag}_events.json"), "w"), ensure_ascii=False, indent=1)
    out_dir = os.path.join(DATA, tag)
    frame_dir = os.path.join(FRAMES, tag)
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(frame_dir, exist_ok=True)
    # 已有结果：时间 -> 文件
    done = {}
    for fp in glob.glob(os.path.join(out_dir, "shot_*.json")):
        try:
            d = json.load(open(fp))
            done[round(d["meta"]["t_center"], 2)] = fp
        except Exception:
            pass
    order = sorted(evs, key=lambda e: -e["peak"])[:limit]
    order.sort(key=lambda e: e["t"])
    print(f"events={len(evs)} target={len(order)} done={len(done)}", flush=True)
    for ev in order:
        hit = None
        for t0, fp in done.items():
            if abs(t0 - ev["t"]) < 0.6:
                hit = fp
                break
        if hit:
            print(f"skip t={ev['t']:.1f} (已有)", flush=True)
            continue
        idx = len(done)
        fp = os.path.join(out_dir, f"shot_{idx:02d}.json")
        while os.path.exists(fp):
            idx += 1
            fp = os.path.join(out_dir, f"shot_{idx:02d}.json")
        t0 = time.time()
        meta, seq = fine_shot(video, sig, ev, f"{tag}{idx:02d}", out_dir, frame_dir)
        json.dump({"meta": meta, "seq": seq}, open(fp, "w"))
        done[round(ev["t"], 2)] = fp
        print(f"shot {idx} t={ev['t']:.1f}s frames={len(seq)} {time.time()-t0:.0f}s", flush=True)
    print("ALL DONE", flush=True)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 20)
