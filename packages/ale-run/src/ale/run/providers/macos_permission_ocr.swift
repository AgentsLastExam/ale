import Foundation
import Vision
import AppKit

// OCR only: reads a supplied VNC screenshot, never captures the host desktop.
guard CommandLine.arguments.count == 2,
      let image = NSImage(contentsOfFile: CommandLine.arguments[1]),
      let cg = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
    exit(2)
}
let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.recognitionLanguages = ["en-US", "zh-Hans"]
request.usesLanguageCorrection = false
try VNImageRequestHandler(cgImage: cg).perform([request])
let rows: [[String: Any]] = (request.results ?? []).compactMap { observation in
    guard let text = observation.topCandidates(1).first else { return nil }
    let b = observation.boundingBox
    return ["text": text.string, "confidence": text.confidence,
            "x": b.minX * Double(cg.width),
            "y": (1 - b.maxY) * Double(cg.height),
            "w": b.width * Double(cg.width), "h": b.height * Double(cg.height)]
}
let data = try JSONSerialization.data(withJSONObject: rows, options: [.sortedKeys])
FileHandle.standardOutput.write(data)
