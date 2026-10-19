// imgtext - one image in, the text Vision reads off it out, as one JSON object.
//
// On-device only: Vision's VNRecognizeTextRequest, accurate, language
// correction on, languages detected automatically. No network API, no
// FoundationModels (tests/test_imgtext.py guards the imports).
//
//   imgtext [--max-pixels N] PATH   -> {"height": H, "lines": [...], "width": W}
//   imgtext --warm                  -> OCRs a small image drawn in memory, so
//                                      Vision loads its model; needs no file
//
// Exit codes: 0 ok, 2 usage, 3 cannot decode, 4 Vision error, 5 over the
// pixel cap. The cap is read from the image header BEFORE anything decodes,
// so a decompression bomb never decodes. Every exit prints one JSON object;
// a failure's object is {"error": "..."}.
//
// The bot compiles this once (swiftc -O) into its root, keyed by this file's
// hash, and runs it with a timeout: src/sessionphone/imgtext.py.
import CoreGraphics
import CoreText
import Foundation
import ImageIO
import Vision

func emit(_ obj: [String: Any], _ code: Int32) -> Never {
  let data = (try? JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys])) ?? Data("{}".utf8)
  FileHandle.standardOutput.write(data)
  FileHandle.standardOutput.write(Data("\n".utf8))
  exit(code)
}

func recognise(_ img: CGImage, _ orientation: CGImagePropertyOrientation) -> Never {
  let req = VNRecognizeTextRequest()
  req.recognitionLevel = .accurate
  req.usesLanguageCorrection = true
  if #available(macOS 13.0, *) { req.automaticallyDetectsLanguage = true }
  let handler = VNImageRequestHandler(cgImage: img, orientation: orientation, options: [:])
  do { try handler.perform([req]) } catch { emit(["error": "vision: \(error.localizedDescription)"], 4) }
  let lines = (req.results ?? []).compactMap { $0.topCandidates(1).first?.string }
  emit(["lines": lines, "width": img.width, "height": img.height], 0)
}

// --warm: draw a line of text in memory and read it, so the first real image
// does not pay for loading Vision's model (25.9 s cold, measured).
func warmImage() -> CGImage? {
  let w = 320, h = 80
  guard let ctx = CGContext(data: nil, width: w, height: h, bitsPerComponent: 8, bytesPerRow: 0,
                            space: CGColorSpaceCreateDeviceRGB(),
                            bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return nil }
  ctx.setFillColor(CGColor(red: 1, green: 1, blue: 1, alpha: 1))
  ctx.fill(CGRect(x: 0, y: 0, width: w, height: h))
  let font = CTFontCreateWithName("Helvetica-Bold" as CFString, 36, nil)
  let attrs: [NSAttributedString.Key: Any] = [
    NSAttributedString.Key(kCTFontAttributeName as String): font,
    NSAttributedString.Key(kCTForegroundColorAttributeName as String): CGColor(red: 0, green: 0, blue: 0, alpha: 1),
  ]
  let line = CTLineCreateWithAttributedString(NSAttributedString(string: "Warm 123", attributes: attrs))
  ctx.textPosition = CGPoint(x: 20, y: 25)
  CTLineDraw(line, ctx)
  return ctx.makeImage()
}

var args = Array(CommandLine.arguments.dropFirst())
let usage = "usage: imgtext [--max-pixels N] PATH | imgtext --warm"

if args == ["--warm"] {
  guard let img = warmImage() else { emit(["error": "cannot draw the warm-up image"], 3) }
  recognise(img, .up)
}

var maxPixels = 50_000_000
if args.count == 3, args[0] == "--max-pixels" {
  guard let n = Int(args[1]), n > 0 else { emit(["error": usage], 2) }
  maxPixels = n
  args.removeFirst(2)
}
guard args.count == 1 else { emit(["error": usage], 2) }

let url = URL(fileURLWithPath: args[0])
guard let src = CGImageSourceCreateWithURL(url as CFURL, nil),
      let props = CGImageSourceCopyPropertiesAtIndex(src, 0, nil) as? [CFString: Any],
      let width = props[kCGImagePropertyPixelWidth] as? Int,
      let height = props[kCGImagePropertyPixelHeight] as? Int
else { emit(["error": "cannot decode image"], 3) }
// width * height can overflow only for absurd headers; multipliedReportingOverflow keeps it honest.
let (pixels, overflow) = width.multipliedReportingOverflow(by: height)
if overflow || pixels > maxPixels {
  emit(["error": "over the pixel cap: \(width)x\(height) > \(maxPixels)", "width": width, "height": height], 5)
}
var orientation = CGImagePropertyOrientation.up
if let raw = props[kCGImagePropertyOrientation] as? UInt32, let o = CGImagePropertyOrientation(rawValue: raw) {
  orientation = o
}
guard let img = CGImageSourceCreateImageAtIndex(src, 0, nil) else { emit(["error": "cannot decode image"], 3) }
recognise(img, orientation)
