"""QA：把粗扫识别到的骨架画到原帧上，用于肉眼校验识别精度"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import Quartz
from bbkit import (make_generator, frame_at, save_jpeg, scale_cg, load_cg,
                   read_jsonl, kp_norm_to_px, frame_features)

EDGES = [
    ("neck_1_joint", "head_joint", "blue"),
    ("neck_1_joint", "right_shoulder_1_joint", "blue"),
    ("neck_1_joint", "left_shoulder_1_joint", "blue"),
    ("right_shoulder_1_joint", "right_forearm_joint", "yellow"),
    ("right_forearm_joint", "right_hand_joint", "green"),
    ("left_shoulder_1_joint", "left_forearm_joint", "yellow"),
    ("left_forearm_joint", "left_hand_joint", "green"),
    ("right_shoulder_1_joint", "right_upLeg_joint", "pink"),
    ("left_shoulder_1_joint", "left_upLeg_joint", "pink"),
    ("right_upLeg_joint", "left_upLeg_joint", "pink"),
    ("right_upLeg_joint", "right_leg_joint", "pink"),
    ("right_leg_joint", "right_foot_joint", "purple"),
    ("left_upLeg_joint", "left_leg_joint", "pink"),
    ("left_leg_joint", "left_foot_joint", "purple"),
]
COLORS = {"blue": (0.30, 0.63, 1.0, 1), "yellow": (1, 0.82, 0.4, 1), "green": (0.25, 0.73, 0.31, 1),
          "pink": (1, 0.56, 0.67, 1), "purple": (0.78, 0.57, 0.92, 1)}


def draw(cg, kp, out_path, max_w=1280):
    W = Quartz.CGImageGetWidth(cg)
    H = Quartz.CGImageGetHeight(cg)
    s = min(1.0, max_w / W)
    cw, ch = int(W * s), int(H * s)
    cs = Quartz.CGColorSpaceCreateDeviceRGB()
    ctx = Quartz.CGBitmapContextCreate(None, cw, ch, 8, cw * 4, cs,
                                       Quartz.kCGImageAlphaPremultipliedFirst | Quartz.kCGBitmapByteOrder32Little)
    Quartz.CGContextDrawImage(ctx, Quartz.CGRectMake(0, 0, cw, ch), cg)

    def P(n):
        v = kp.get(n)
        return (v[0] * s, v[1] * s) if v else None

    Quartz.CGContextSetLineWidth(ctx, 4)
    Quartz.CGContextSetLineCap(ctx, Quartz.kCGLineCapRound)
    for a, b, col in EDGES:
        pa, pb = P(a), P(b)
        if not (pa and pb):
            continue
        r, g, bl, al = COLORS[col]
        Quartz.CGContextSetRGBStrokeColor(ctx, r, g, bl, al)
        Quartz.CGContextBeginPath(ctx)
        Quartz.CGContextMoveToPoint(ctx, pa[0], ch - pa[1])
        Quartz.CGContextAddLineToPoint(ctx, pb[0], ch - pb[1])
        Quartz.CGContextStrokePath(ctx)
    for k, v in kp.items():
        x, y = v[0] * s, ch - v[1] * s
        Quartz.CGContextSetRGBFillColor(ctx, 1, 1, 1, 0.95)
        Quartz.CGContextFillEllipseInRect(ctx, Quartz.CGRectMake(x - 4, y - 4, 8, 8))
    img = Quartz.CGBitmapContextCreateImage(ctx)
    save_jpeg(img, out_path, 0.85)
    return out_path


if __name__ == "__main__":
    video, coarse, times = sys.argv[1], sys.argv[2], [float(x) for x in sys.argv[3].split(",")]
    recs = [r for r in read_jsonl(coarse) if r.get("kp")]
    gen, _ = make_generator(video, (1920, 1080))
    outd = os.path.join(os.path.dirname(coarse), "qa")
    os.makedirs(outd, exist_ok=True)
    for t in times:
        r = min(recs, key=lambda r: abs(r["t"] - t))
        cg = frame_at(gen, r["t"])
        kp = r["kp"]
        W = r.get("W") or 0
        H = r.get("H") or 0
        W = W if W > 1 else 1920
        H = H if H > 1 else 1080
        kp = {k: [v[0] * W, v[1] * H, v[2]] for k, v in kp.items()}
        p = draw(cg, kp, os.path.join(outd, f"qa_{r['t']:.1f}.jpg"))
        f = frame_features({"t": r["t"], "i": 0, "kp": kp})
        print(p, "bh=%.0f" % f["body_h"],
              "R wr_s=%.2f" % (f["right"]["wr_over_sh"] or 0),
              "L wr_s=%.2f" % (f["left"]["wr_over_sh"] or 0))
