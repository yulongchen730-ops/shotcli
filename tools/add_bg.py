# -*- coding: utf-8 -*-
"""给已合成的口播视频加「原视频原声」当背景音（跟画面同步慢放）。

用法: python add_bg.py <口播视频> <原视频> <tag> [音量 0~1]

做法：读 deep60 序列算出每球的 t0 / 屈膝帧 t_dip / 出手 t_rel，把原声按「定格前 / 定格后」
两段做 2 倍慢放铺到对应球段，定格段静音 —— 这样球进网那声「砰」正好落在出手后 1.2s。
背景音音量默认 0.25，口播保持 1.0。
"""
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import AVFoundation as AVF
import CoreMedia as CM
import Foundation as F
from Foundation import NSURL
import report as R

VO_VID = sys.argv[1]
SRC_VID = sys.argv[2]
TAG = sys.argv[3]
BG_VOL = float(sys.argv[4]) if len(sys.argv) > 4 else 0.25

FREEZE = 2.6        # 关键帧定格时长（秒），背景音在此段静音
POST = 1.4          # 出手后保留时长，与 makevid.POST_CUT 一致


def dur_of(path):
    a = AVF.AVURLAsset.alloc().initWithURL_options_(NSURL.fileURLWithPath_(path), None)
    d = a.duration()
    return d.value / d.timescale


def knee_min_t(seq):
    """找屈膝角最小的帧时刻（= 屈膝最低点，即关键帧定格位置）"""
    best = None
    for r in seq:
        f = R.frame_features(r)
        if not f:
            continue
        kn = [(f.get(s) or {}).get("knee") for s in ("right", "left")]
        kn = [v for v in kn if v is not None]
        if kn:
            k = sum(kn) / len(kn)
            if best is None or k < best[0]:
                best = (k, r["t"])
    return best[1] if best else None


def build():
    # 1) 每球的 t0 / t_dip / t_rel（按出手时间排序）
    segs = []
    for fp in sorted(glob.glob(os.path.join(R.DATA, TAG, "deep60_*.json"))):
        dj = json.load(open(fp))
        seq, meta = dj["seq"], dj["meta"]
        t_rel = meta.get("t_center")
        if t_rel is None:
            continue
        t_dip = knee_min_t(seq)
        if t_dip is None:
            t_dip = t_rel - 0.53
        segs.append((seq[0]["t"], t_dip, t_rel))
    segs.sort(key=lambda x: x[2])
    total = dur_of(VO_VID)
    seg_starts = [round(total * i / len(segs), 2) for i in range(len(segs))]
    print("球段（t0, t_dip, t_rel → 铺放起点）:")
    for (t0, td, tr), ss in zip(segs, seg_starts):
        print("  %.2f / %.2f / %.2f → %.2fs" % (t0, td, tr, ss))

    # 2) 合成：视频轨 + 口播音轨（从口播视频整段复制）+ 背景音轨
    vo = AVF.AVURLAsset.alloc().initWithURL_options_(NSURL.fileURLWithPath_(VO_VID), None)
    src = AVF.AVURLAsset.alloc().initWithURL_options_(NSURL.fileURLWithPath_(SRC_VID), None)
    src_audio = src.tracksWithMediaType_(AVF.AVMediaTypeAudio)[0]

    comp = AVF.AVMutableComposition.composition()
    # 视频轨 + 口播音轨：整段照搬
    vt = comp.addMutableTrackWithMediaType_preferredTrackID_(AVF.AVMediaTypeVideo, 1)
    vt.insertTimeRange_ofTrack_atTime_error_(
        CM.CMTimeRangeMake(CM.CMTimeMake(0, 1), vo.duration()),
        vo.tracksWithMediaType_(AVF.AVMediaTypeVideo)[0], CM.CMTimeMake(0, 1), None)
    vo_audio = vo.tracksWithMediaType_(AVF.AVMediaTypeAudio)
    if vo_audio:
        at = comp.addMutableTrackWithMediaType_preferredTrackID_(AVF.AVMediaTypeAudio, 2)
        at.insertTimeRange_ofTrack_atTime_error_(
            CM.CMTimeRangeMake(CM.CMTimeMake(0, 1), vo.duration()), vo_audio[0], CM.CMTimeMake(0, 1), None)

    # 背景音轨：每球两段慢放（定格段静音）
    bg = comp.addMutableTrackWithMediaType_preferredTrackID_(AVF.AVMediaTypeAudio, 3)
    for (t0, td, tr), ss in zip(segs, seg_starts):
        t_end = tr + POST
        # 段1：屈膝蓄力 [t0, t_dip] 慢放 2 倍
        d1 = max(0.05, td - t0)
        rng1 = CM.CMTimeRangeMake(CM.CMTimeMakeWithSeconds(t0, 600), CM.CMTimeMakeWithSeconds(d1, 600))
        bg.insertTimeRange_ofTrack_atTime_error_(rng1, src_audio, CM.CMTimeMakeWithSeconds(ss, 600), None)
        bg.scaleTimeRange_toDuration_(rng1, CM.CMTimeMakeWithSeconds(d1 * 2, 600))
        # 段2：起跳出手落地 [t_dip, t_end] 慢放，起点跳过定格段
        d2 = max(0.05, t_end - td)
        rng2 = CM.CMTimeRangeMake(CM.CMTimeMakeWithSeconds(td, 600), CM.CMTimeMakeWithSeconds(d2, 600))
        at2 = CM.CMTimeMakeWithSeconds(ss + d1 * 2 + FREEZE, 600)
        bg.insertTimeRange_ofTrack_atTime_error_(rng2, src_audio, at2, None)
        bg.scaleTimeRange_toDuration_(rng2, CM.CMTimeMakeWithSeconds(d2 * 2, 600))

    # 3) 混音：背景音压低，口播保持 1.0（不列 inputParameters 即默认音量）
    mix = AVF.AVMutableAudioMix.alloc().init()
    params = AVF.AVMutableAudioMixInputParameters.audioMixInputParametersWithTrack_(bg)
    params.setVolume_atTime_(BG_VOL, CM.CMTimeMake(0, 1))
    mix.setInputParameters_([params])

    # 4) 导出
    out = os.path.splitext(VO_VID)[0] + "_原声.mp4"
    if os.path.exists(out):
        os.remove(out)
    exp = AVF.AVAssetExportSession.alloc().initWithAsset_presetName_(
        comp, AVF.AVAssetExportPresetHighestQuality)
    exp.setOutputURL_(NSURL.fileURLWithPath_(out))
    exp.setOutputFileType_(AVF.AVFileTypeMPEG4)
    exp.setAudioMix_(mix)
    exp.exportAsynchronouslyWithCompletionHandler_(None)
    while exp.status() in (1, 2):
        F.NSRunLoop.currentRunLoop().runMode_beforeDate_(
            F.NSDefaultRunLoopMode, F.NSDate.dateWithTimeIntervalSinceNow_(0.2))
    print("导出状态", exp.status(), exp.error(), flush=True)
    from bbkit import probe
    print("OK" if os.path.exists(out) else "FAIL", out, probe(out) if os.path.exists(out) else "")
    return out


if __name__ == "__main__":
    build()
