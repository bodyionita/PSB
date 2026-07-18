// Client-side HEIC→JPEG (M9 T5, ADR-060 §8): Chrome/Android can't decode HEIC and the VLM needs a
// renderable image, so a HEIC pick is converted to JPEG before upload. The heavy libheif wasm
// converter is dynamically imported only when a HEIC/HEIF file is actually picked — it never loads
// on the common jpg/png path. The converted JPEG *becomes* the raw (a synthetic `photo.jpg` filename
// per the upload contract — the server derives mime from the extension and 400s a nameless blob); the
// camera-original HEIC is not kept (ADR-060 §8, and is itself re-derivable forever).

const HEIC_EXTS = ['heic', 'heif'];

function ext(name: string): string {
  const i = name.lastIndexOf('.');
  return i >= 0 ? name.slice(i + 1).toLowerCase() : '';
}

export function isHeic(file: File): boolean {
  const type = file.type.toLowerCase();
  return type.includes('heic') || type.includes('heif') || HEIC_EXTS.includes(ext(file.name));
}

// A file picked from the camera/library, normalized for upload. HEIC/HEIF → JPEG (`photo.jpg`);
// everything else passes through with its own blob + filename. A conversion failure throws so the
// caller can surface it inline rather than uploading a file the browser/VLM can't read.
export async function toUploadable(file: File): Promise<{ blob: Blob; filename: string }> {
  if (!isHeic(file)) return { blob: file, filename: file.name };
  const { default: heic2any } = await import('heic2any');
  const out = await heic2any({ blob: file, toType: 'image/jpeg', quality: 0.9 });
  const blob = Array.isArray(out) ? out[0] : out;
  return { blob: blob as Blob, filename: 'photo.jpg' };
}
