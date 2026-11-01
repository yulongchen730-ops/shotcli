"""把若干帧拼成一张 contact sheet，用于快速肉眼校验"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import Quartz
from bbkit import save_jpeg, load_cg


def montage(paths, out_path, cols=5, cell=(300, 380)):
    rows = (len(paths) + cols - 1) // cols
    cw, chh = cell
    W, H = cols * cw, rows * chh
    cs = Quartz.CGColorSpaceCreateDeviceRGB()
    ctx = Quartz.CGBitmapContextCreate(None, W, H, 8, W * 4, cs,
                                       Quartz.kCGImageAlphaPremultipliedFirst | Quartz.kCGBitmapByteOrder32Little)
    Quartz.CGContextSetRGBFillColor(ctx, 0.05, 0.07, 0.1, 1)
    Quartz.CGContextFillRect(ctx, Quartz.CGRectMake(0, 0, W, H))
    for i, p in enumerate(paths):
        cg = load_cg(p)
        if cg is None:
            continue
        r, c = divmod(i, cols)
        x, y = c * cw, H - (r + 1) * chh
        iw, ih = Quartz.CGImageGetWidth(cg), Quartz.CGImageGetHeight(cg)
        s = min((cw - 6) / iw, (chh - 6) / ih)
        dw, dh = iw * s, ih * s
        Quartz.CGContextDrawImage(ctx, Quartz.CGRectMake(x + (cw - dw) / 2, y + (chh - dh) / 2, dw, dh), cg)
    save_jpeg(Quartz.CGBitmapContextCreateImage(ctx), out_path, 0.85)
    return out_path


if __name__ == "__main__":
    out = sys.argv[1]
    print(montage(sys.argv[2:], out))
