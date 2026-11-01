# -*- coding: utf-8 -*-
"""给合集视频铺口播（TTS 生成的三段旁白，按三个球的时间段插入）。

用法: python add_vo.py <视频> <口播目录> [段落起点(逗号分隔，秒)]
段落起点默认按视频时长三等分，也可以显式给（例如 0.3,9.7,16.5）。
"""
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import AVFoundation as AVF
import CoreMedia as CM
import Foundation as F
from Foundation import NSURL

VID = sys.argv[1]
VODIR = sys.argv[2] if len(sys.argv) > 2 else "/tmp"
STARTS = [float(x) for x in sys.argv[3].split(",")] if len(sys.argv) > 3 else None


def dur_of(path):
    a = AVF.AVURLAsset.alloc().initWithURL_options_(NSURL.fileURLWithPath_(path), None)
    d = a.duration()
    return d.value / d.timescale


def build():
    vid_asset = AVF.AVURLAsset.alloc().initWithURL_options_(NSURL.fileURLWithPath_(VID), None)
    total = dur_of(VID)
    # 真人录音常用 .m4a/.wav/.mp3，TTS 用 .aiff —— 都收进来，按文件名排序即为播放顺序
    clips = []
    for ext in ("aiff", "m4a", "wav", "mp3", "caf"):
        clips += glob.glob(os.path.join(VODIR, "*." + ext))
    clips = sorted(clips)
    if not clips:
        raise SystemExit("目录里没有可用的口播音频（.aiff/.m4a/.wav/.mp3）：%s" % VODIR)
    starts = STARTS or [round(total * i / len(clips), 2) for i in range(len(clips))]

    comp = AVF.AVMutableComposition.composition()
    vt = comp.addMutableTrackWithMediaType_preferredTrackID_(AVF.AVMediaTypeVideo, 1)
    va = vid_asset.tracksWithMediaType_(AVF.AVMediaTypeVideo)[0]
    vt.insertTimeRange_ofTrack_atTime_error_(
        CM.CMTimeRangeMake(CM.CMTimeMake(0, 1), vid_asset.duration()), va, CM.CMTimeMake(0, 1), None)

    at = comp.addMutableTrackWithMediaType_preferredTrackID_(AVF.AVMediaTypeAudio, 2)
    for clip, t0 in zip(clips, starts):
        a = AVF.AVURLAsset.alloc().initWithURL_options_(NSURL.fileURLWithPath_(clip), None)
        src = a.tracksWithMediaType_(AVF.AVMediaTypeAudio)
        if not src:
            print("  跳过（无音轨）", os.path.basename(clip), flush=True)
            continue
        d = a.duration()
        d_sec = d.value / d.timescale
        remain = total - t0
        if d_sec > remain + 0.02:      # 音频超出视频末尾会被裁掉（否则合成后视频尾部黑屏）
            d = CM.CMTimeMakeWithSeconds(max(0.0, remain), 600)
            print("    （音频 %.2fs 超出视频末尾 %.2fs，自动截断）" % (d_sec, remain), flush=True)
        ok = at.insertTimeRange_ofTrack_atTime_error_(
            CM.CMTimeRangeMake(CM.CMTimeMake(0, 1), d), src[0],
            CM.CMTimeMakeWithSeconds(t0, 600), None)[0]
        print("  %-16s 起点 %5.2fs  时长 %5.2fs  %s" % (
            os.path.basename(clip), t0, d.value / d.timescale, "ok" if ok else "FAIL"), flush=True)

    out = os.path.splitext(VID)[0] + "_口播.mp4"
    if os.path.exists(out):
        os.remove(out)
    exp = AVF.AVAssetExportSession.alloc().initWithAsset_presetName_(
        comp, AVF.AVAssetExportPresetHighestQuality)
    exp.setOutputURL_(NSURL.fileURLWithPath_(out))
    exp.setOutputFileType_(AVF.AVFileTypeMPEG4)
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
