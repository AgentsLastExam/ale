// Parse saved RTF attributes using AppKit; no application automation or GUI access.
ObjC.import('AppKit');

function run(argv) {
    const error = Ref();
    const doc = $.NSAttributedString.alloc.initWithURLOptionsDocumentAttributesError(
        $.NSURL.fileURLWithPath(argv[0]), $.NSDictionary.dictionary, Ref(), error
    );
    if (!doc || doc.isNil()) throw new Error('Cannot decode RTF');
    const text = ObjC.unwrap(doc.string);
    const lines = [];
    const pattern = /[^\r\n]+/g;
    let match;
    while ((match = pattern.exec(text)) !== null) {
        const styles = new Map();
        for (let i = 0; i < match[0].length; i++) {
            if (/\s/.test(match[0][i])) continue;
            const attrs = doc.attributesAtIndexEffectiveRange(match.index + i, Ref());
            const font = attrs.objectForKey($.NSFontAttributeName);
            const paragraph = attrs.objectForKey($.NSParagraphStyleAttributeName);
            const style = {
                size: Number(font.pointSize),
                bold: (Number($.NSFontManager.sharedFontManager.traitsOfFont(font)) & 2) !== 0,
                alignment: paragraph.isNil() ? 0 : Number(paragraph.alignment)
            };
            styles.set(JSON.stringify(style), style);
        }
        lines.push({text: match[0], styles: Array.from(styles.values())});
    }
    return JSON.stringify(lines);
}
