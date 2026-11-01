"""bbkit — 投篮视频分析工具库（纯 macOS 原生：AVFoundation 解码 + Vision 人体姿态）

依赖：pyobjc-framework-AVFoundation / CoreMedia / Quartz / Vision  （pip install，无需 ffmpeg/opencv）
坐标约定：Vision 输出归一化坐标(原点左下) -> 统一转成像素坐标(原点左上)
"""
import os
import json
import math
import time
from math import hypot, degrees, acos, atan2

import Quartz
import CoreMedia as CM
import AVFoundation as AVF
import Vision
from Foundation import NSURL

# ---------------------------------------------------------------- 关节名（Vision 19 点模型）
SHOULDER = "shoulder_1_joint"
ELBOW = "forearm_joint"
WRIST = "hand_joint"
HIP = "upLeg_joint"
KNEE = "leg_joint"
ANKLE = "foot_joint"
NECK = "neck_1_joint"
HEAD = "head_joint"

SIDES = ("right", "left")

# ---------------------------------------------------------------------------- 视频读取


def make_generator(path, max_size=(1920, 1080)):
    """随机访问抽帧（用于精细分析）"""
    url = NSURL.fileURLWithPath_(path)
    asset = AVF.AVURLAsset.alloc().initWithURL_options_(url, None)
    gen = AVF.AVAssetImageGenerator.alloc().initWithAsset_(asset)
    gen.setAppliesPreferredTrackTransform_(True)
    gen.setMaximumSize_((max_size[0], max_size[1]))
    gen.setRequestedTimeToleranceBefore_(CM.CMTimeMake(1, 300))
    gen.setRequestedTimeToleranceAfter_(CM.CMTimeMake(1, 300))
    return gen, asset


def probe(path):
    url = NSURL.fileURLWithPath_(path)
    asset = AVF.AVURLAsset.alloc().initWithURL_options_(url, None)
    dur = asset.duration()
    secs = dur.value / dur.timescale
    tracks = asset.tracksWithMediaType_(AVF.AVMediaTypeVideo)
    info = {"path": path, "duration": round(secs, 2)}
    if tracks:
        t = tracks[0]
        sz = t.naturalSize()
        info["size"] = [int(sz.width), int(sz.height)]
        info["fps"] = round(t.nominalFrameRate(), 2)
    return info


def frame_at(gen, t_sec):
    tm = CM.CMTimeMakeWithSeconds(t_sec, 600)
    res = gen.copyCGImageAtTime_actualTime_error_(tm, None, None)
    if isinstance(res, tuple):
        cg = res[0] if len(res) == 3 else res[0]
        return cg
    return res


def save_jpeg(cg, path, quality=0.8):
    url = NSURL.fileURLWithPath_(path)
    dest = Quartz.CGImageDestinationCreateWithURL(url, "public.jpeg", 1, None)
    Quartz.CGImageDestinationAddImage(dest, cg, {Quartz.kCGImageDestinationLossyCompressionQuality: quality})
    return Quartz.CGImageDestinationFinalize(dest)


def load_cg(path):
    url = NSURL.fileURLWithPath_(path)
    src = Quartz.CGImageSourceCreateWithURL(url, None)
    if src is None:
        return None
    return Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)


def crop_cg(cg, rect):
    return Quartz.CGImageCreateWithImageInRect(cg, rect)


def scale_cg(cg, out_w, out_h):
    cs = Quartz.CGColorSpaceCreateDeviceRGB()
    ctx = Quartz.CGBitmapContextCreate(None, int(out_w), int(out_h), 8, int(out_w) * 4, cs,
                                       Quartz.kCGImageAlphaPremultipliedFirst | Quartz.kCGBitmapByteOrder32Little)
    Quartz.CGContextSetInterpolationQuality(ctx, Quartz.kCGInterpolationHigh)
    Quartz.CGContextDrawImage(ctx, Quartz.CGRectMake(0, 0, out_w, out_h), cg)
    return Quartz.CGBitmapContextCreateImage(ctx)


# ------------------------------------------------------------------ 顺序解码扫描（粗扫，快）


def iter_frames(path, t_start=0.0, t_end=None):
    """顺序解码，逐帧回调 → (t, sampleBuffer)。比随机访问快得多。"""
    url = NSURL.fileURLWithPath_(path)
    asset = AVF.AVURLAsset.alloc().initWithURL_options_(
        url, {AVF.AVURLAssetPreferPreciseDurationAndTimingKey: True})
    track = asset.tracksWithMediaType_(AVF.AVMediaTypeVideo)[0]
    reader, err = AVF.AVAssetReader.alloc().initWithAsset_error_(asset, None)
    out = AVF.AVAssetReaderTrackOutput.alloc().initWithTrack_outputSettings_(track, {"PixelFormatType": 32})
    out.setAlwaysCopiesSampleData_(False)
    reader.addOutput_(out)
    reader.startReading()
    while True:
        sb = out.copyNextSampleBuffer()
        if sb is None:
            break
        pts = CM.CMSampleBufferGetPresentationTimeStamp(sb)
        t = pts.value / pts.timescale
        if t < t_start:
            continue
        if t_end is not None and t > t_end:
            break
        yield t, sb
    reader.cancelReading()


# ------------------------------------------------------------------------- 姿态估计


class Poser:
    """Vision 人体姿态；支持 CGImage 与 CMSampleBuffer 两种输入"""

    def __init__(self):
        self.req = Vision.VNDetectHumanBodyPoseRequest.alloc().init()

    def _collect(self):
        out = []
        for obs in self.req.results():
            d = {}
            for name in obs.availableJointNames():
                r = obs.recognizedPointForJointName_error_(name, None)
                v = r[0] if isinstance(r, tuple) else r
                if v is None:
                    continue
                x, y, c = v.location().x, v.location().y, v.confidence()
                if c > 0.05:
                    d[str(name)] = (x, y, c)
            out.append(d)
        return out

    def detect_sb(self, sb):
        h = Vision.VNImageRequestHandler.alloc().initWithCMSampleBuffer_options_(sb, None)
        ok, err = h.performRequests_error_([self.req], None)
        if not ok:
            return []
        return self._collect()

    def detect_cg(self, cg):
        h = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(cg, None)
        ok, err = h.performRequests_error_([self.req], None)
        if not ok:
            return []
        return self._collect()


def kp_norm_to_px(kp, W, H):
    """Vision 归一化(原点左下) -> 像素(原点左上)"""
    return {k: [v[0] * W, (1 - v[1]) * H, v[2]] for k, v in kp.items()}


def pick_person(dets, prev_cx=None):
    """选主体：优先与上一帧连续，否则取包围盒最大的人"""
    if not dets:
        return None

    def cx(d):
        xs = [v[0] for v in d.values()]
        return (max(xs) + min(xs)) / 2

    def area(d):
        xs = [v[0] for v in d.values()]
        ys = [v[1] for v in d.values()]
        return (max(xs) - min(xs)) * (max(ys) - min(ys))

    if prev_cx is not None:
        d = min(dets, key=lambda d: abs(cx(d) - prev_cx))
        if abs(cx(d) - prev_cx) < 0.25:
            return d
    return max(dets, key=area)


# ------------------------------------------------------------------------- 几何/运动学


def joint(kp, name):
    v = kp.get(name)
    return (v[0], v[1]) if v else None


def angle3(a, b, c):
    """b 处的关节角（度）"""
    if not (a and b and c):
        return None
    v1 = (a[0] - b[0], a[1] - b[1])
    v2 = (c[0] - b[0], c[1] - b[1])
    n1, n2 = hypot(*v1), hypot(*v2)
    if n1 < 1e-6 or n2 < 1e-6:
        return None
    return degrees(acos(max(-1.0, min(1.0, (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)))))


def angle_from_horizontal(a, b):
    """向量 a->b 与水平线的夹角（度，图像 y 向下，取正值表示向上抬）"""
    if not (a and b):
        return None
    dx = b[0] - a[0]
    dy = a[1] - b[1]  # 向上为正
    return degrees(atan2(dy, abs(dx) if abs(dx) > 1e-6 else 1e-6))


def body_height(kp):
    head = joint(kp, HEAD) or joint(kp, NECK)
    ankles = [joint(kp, s + "_" + ANKLE) for s in SIDES]
    ankles = [a for a in ankles if a]
    if head and ankles:
        h = max(abs(a[1] - head[1]) for a in ankles)
        if h > 20:
            return h
    ys = [v[1] for v in kp.values()]
    return max(ys) - min(ys) or 1.0


def frame_features(rec):
    """逐帧派生指标"""
    kp = rec.get("kp")
    if not kp:
        return None
    bh = body_height(kp)
    head = joint(kp, HEAD) or joint(kp, NECK)
    out = {"t": rec["t"], "i": rec["i"], "img": rec.get("img"), "body_h": bh,
           "cw": rec.get("cw"), "ch": rec.get("ch"), "kp": kp}
    for side in SIDES:
        sh = joint(kp, side + "_" + SHOULDER)
        el = joint(kp, side + "_" + ELBOW)
        wr = joint(kp, side + "_" + WRIST)
        hp = joint(kp, side + "_" + HIP)
        kn = joint(kp, side + "_" + KNEE)
        an = joint(kp, side + "_" + ANKLE)
        out[side] = {
            "elbow": angle3(sh, el, wr),
            "knee": angle3(hp, kn, an),
            "wr_over_sh": ((sh[1] - wr[1]) / bh) if (sh and wr) else None,
            "wr_over_ankle": ((an[1] - wr[1]) / bh) if (an and wr) else None,
            "wr_over_head": ((head[1] - wr[1]) / bh) if (head and wr) else None,
            "fore_ang": angle_from_horizontal(el, wr),
            "push_ang": angle_from_horizontal(sh, wr),
            "sh": sh, "el": el, "wr": wr, "hp": hp, "kn": kn, "an": an,
        }
    return out


# --------------------------------------------------------------------------- 文件工具


def append_jsonl(path, obj):
    with open(path, "a") as f:
        f.write(json.dumps(obj) + "\n")


def read_jsonl(path):
    if not os.path.exists(path):
        return []
    out = []
    for line in open(path):
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def hr(t):
    return f"{int(t // 60)}:{t % 60:04.1f}"
