# -*- coding: utf-8 -*-
"""把最佳三球的慢放视频按指定顺序拼成一条（默认 3→2→1，第 1 名压轴）。

三个片子的分辨率/帧率/编码一致（都是 AVAssetWriter 出的 30fps H.264），所以直接用
AVMutableComposition 拼接即可，不需要重新渲染。

用法: python join_shots.py [顺序，如 3,2,1] [v|h]
"""
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import AVFoundation as AVF
import CoreMedia as CM
import Foundation as F
from Foundation import NSURL
import report as R

ORDER = (sys.argv[1] if len(sys.argv) > 1 else "3,2,1").split(",")
KIND = sys.argv[2] if len(sys.argv) > 2 else "v"
TAG = sys.argv[3] if len(sys.argv) > 3 else "f"
TOPSUF = sys.argv[4] if len(sys.argv) > 4 else "top3"
LABEL = "名" if TOPSUF.startswith("top") else "球"
SUF = sys.argv[5] if len(sys.argv) > 5 else ""       # 与 makevid 的 DEEP_SUF 一致（如 "_慢60"）
# 容错：调用方常直接传深扫后缀（"60"），而文件名里是 makevid 拼的 "_慢60"。
# 不归一化的话 find() 的精确路径会对不上（靠 glob 兜底才没出事），
# 且合集名会变成「合集_1_2_360」这种把顺序和后缀粘在一起的名字。
if SUF and not SUF.startswith("_"):
    SUF = "_慢" + SUF
R.apply_tag(TAG)                                     # 取 R.REPORT_DATE 拼文件名（与 makevid 同一契约）
DATE = R.REPORT_DATE
SRC = os.path.join(R.ROOT, "视频") if KIND == "v" else os.path.join(R.ROOT, "视频", "横版")


def find(rank, top3, label="名", suf=""):
    """按 <tag>_top3.json 里的时间/评分精确构造文件名。

    文件名带日期前缀后，跨日期串片的问题被根治（9月13日 和 9月20日 的
    「第1名」前缀原本完全相同，只能靠 mtime 猜，现在日期段天然区分）。
    glob 兜底也带日期，取不到就返回 None，绝不会错拿别的训练日。
    """
    if top3:
        it = top3[int(rank) - 1]
        p = os.path.join(SRC, "%s_第%d%s_出手%.1fs_评分%.0f%s.mp4" % (
            DATE, int(rank), label, it["t"], it["score"], suf))
        if os.path.exists(p):
            return p
    hit = sorted(glob.glob(os.path.join(SRC, "%s_第%s%s*%s.mp4" % (DATE, rank, label, suf))),
                 key=os.path.getmtime)
    if len(hit) > 1:
        print("  ! 第%s%s 有多个候选，取最新的：%s" % (rank, label, os.path.basename(hit[-1])), flush=True)
    return hit[-1] if hit else None


def join(paths, out):
    comp = AVF.AVMutableComposition.composition()
    vt = comp.addMutableTrackWithMediaType_preferredTrackID_(AVF.AVMediaTypeVideo, 1)
    cur = CM.CMTimeMake(0, 1)
    for p in paths:
        asset = AVF.AVURLAsset.alloc().initWithURL_options_(NSURL.fileURLWithPath_(p), None)
        at = asset.tracksWithMediaType_(AVF.AVMediaTypeVideo)[0]
        dur = asset.duration()
        ok = vt.insertTimeRange_ofTrack_atTime_error_(
            CM.CMTimeRangeMake(CM.CMTimeMake(0, 1), dur), at, cur, None)[0]
        print("  insert %-40s %5.2fs  %s" % (
            os.path.basename(p), dur.value / dur.timescale, "ok" if ok else "FAIL"), flush=True)
        cur = CM.CMTimeAdd(cur, dur)
    if os.path.exists(out):
        os.remove(out)
    exp = AVF.AVAssetExportSession.alloc().initWithAsset_presetName_(
        comp, AVF.AVAssetExportPresetHighestQuality)
    exp.setOutputURL_(NSURL.fileURLWithPath_(out))
    exp.setOutputFileType_(AVF.AVFileTypeMPEG4)
    exp.exportAsynchronouslyWithCompletionHandler_(None)
    while exp.status() in (1, 2):          # waiting / exporting
        F.NSRunLoop.currentRunLoop().runMode_beforeDate_(
            F.NSDefaultRunLoopMode, F.NSDate.dateWithTimeIntervalSinceNow_(0.2))
    print("导出状态", exp.status(), exp.error(), flush=True)
    return os.path.exists(out)


def main():
    import json
    ft = os.path.join(R.DATA, "%s_%s.json" % (TAG, TOPSUF))
    top3 = json.load(open(ft)) if os.path.exists(ft) else None
    paths = [p for p in (find(r, top3, LABEL, SUF) for r in ORDER) if p]
    print("拼接顺序", [os.path.basename(p) for p in paths], flush=True)
    out = os.path.join(SRC, "%s_合集_%s%s.mp4" % (DATE, "_".join(ORDER), SUF))
    if not join(paths, out):
        raise SystemExit("拼接失败")
    from bbkit import probe
    print("OK", out, probe(out), "%.1f MB" % (os.path.getsize(out) / 1024 / 1024), flush=True)


if __name__ == "__main__":
    main()
