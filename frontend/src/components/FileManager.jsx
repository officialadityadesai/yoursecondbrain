import { useState, useEffect, useRef } from 'react';
import { Database, Trash2, FileText, FileVideo, FileImage, UploadCloud, Loader2, CheckCircle, XCircle } from 'lucide-react';
import axios from 'axios';

const NOTE_FILENAME_RE = /^note_[a-f0-9]{10,32}\.md$/i;

export function FileManager({ onPreview, onDataChanged, onOpenBrainDumpNote, refreshKey = 0 }) {
  const [files, setFiles] = useState([]);
  const [isLoading, setIsLoading] = useState(true);
  const [isUploading, setIsUploading] = useState(false);
  const [uploadStatus, setUploadStatus] = useState(null); // { type: 'info'|'error'|'success', text: string }
  const [pendingUploads, setPendingUploads] = useState([]); // names currently being processed by backend
  const [uploadProgress, setUploadProgress] = useState(0);
  const [uploadPhaseLabel, setUploadPhaseLabel] = useState('');
  const [queuedFiles, setQueuedFiles] = useState([]);
  const [uploadContext, setUploadContext] = useState('');
  const [showIngestPanel, setShowIngestPanel] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState(null);
  const fileInputRef = useRef(null);

  const safeSetProgress = (next) => {
    const value = Math.max(0, Math.min(100, Math.round(next)));
    setUploadProgress((prev) => Math.max(prev, value));
  };

  const fetchFiles = async () => {
    try {
      const res = await axios.get('/api/files');
      const list = res.data.files || [];
      setFiles(list);
      return list;
    } catch (err) {
      console.error(err);
      return null;
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => { fetchFiles(); }, []);
  useEffect(() => { if (refreshKey > 0) fetchFiles(); }, [refreshKey]);

  // Poll pending uploads
  useEffect(() => {
    if (pendingUploads.length === 0) {
      if (!isUploading) setUploadProgress(0);
      return;
    }

    const interval = setInterval(async () => {
      const checks = await Promise.all(
        pendingUploads.map(async (name) => {
          try {
            const res = await axios.get(`/api/upload-status/${encodeURIComponent(name)}`);
            return { name, status: res.data?.status || 'unknown', error: res.data?.error || null };
          } catch {
            return { name, status: 'unknown', error: null };
          }
        })
      );

      const failed  = checks.filter(c => c.status === 'failed');
      const done    = checks.filter(c => c.status === 'done');
      const still   = checks.filter(c => c.status !== 'done' && c.status !== 'failed');

      setPendingUploads(still.map(c => c.name));

      if (still.length > 0) {
        const activeFile = still.find(c => c.status === 'processing');
        const queuedCount = still.filter(c => c.status === 'queued').length;
        if (activeFile) {
          const queuedSuffix = queuedCount > 0 ? ` (${queuedCount} queued)` : '';
          setUploadPhaseLabel(`Processing: ${activeFile.name}${queuedSuffix}`);
        } else {
          setUploadPhaseLabel(`Queued: ${still.length} file${still.length !== 1 ? 's' : ''} waiting…`);
        }
        const total = checks.length;
        const completedNow = done.length + failed.length;
        safeSetProgress(60 + (completedNow / total) * 38);
      }

      if (failed.length > 0) {
        const msg = failed.map(f => `${f.name}: ${f.error || 'Processing failed'}`).join(' · ');
        setUploadStatus({ type: 'error', text: msg });
      }

      if (still.length === 0) {
        safeSetProgress(100);
        if (failed.length === 0) {
          setUploadStatus({ type: 'success', text: `${done.length} file${done.length !== 1 ? 's' : ''} ingested successfully.` });
        }
        setUploadPhaseLabel('');
        setTimeout(() => {
          setUploadProgress(0);
          setUploadStatus(null);
        }, 3000);
        await fetchFiles();
        if (onDataChanged) onDataChanged();
      } else if (done.length > 0 || failed.length > 0) {
        await fetchFiles();
        if (onDataChanged) onDataChanged();
      }
    }, 2000);

    return () => clearInterval(interval);
  }, [pendingUploads, onDataChanged, isUploading]);

  const performDelete = async (filename) => {
    try {
      await axios.delete(`/api/files/${encodeURIComponent(filename)}`);
      // Clear from both local state and any in-flight processing state
      setFiles(f => f.filter(x => x.name !== filename));
      setPendingUploads(prev => prev.filter(n => n !== filename));
      // Refetch from server so duplicate checks see the true current state
      await fetchFiles();
      if (onDataChanged) onDataChanged();
    } catch (err) {
      console.error(err);
      setUploadStatus({ type: 'error', text: `Delete failed: ${filename}` });
    }
  };

  const handleDelete = (filename) => setDeleteTarget(filename);
  const confirmDelete = async () => {
    if (!deleteTarget) return;
    const t = deleteTarget;
    setDeleteTarget(null);
    await performDelete(t);
  };

  const handleUploadClick = () => fileInputRef.current?.click();

  const quickFingerprint = async (file) => {
    const headSize = Math.min(file.size, 65536);
    const head = await file.slice(0, headSize).arrayBuffer();
    const digest = await crypto.subtle.digest('SHA-256', head);
    const shortHash = Array.from(new Uint8Array(digest)).slice(0, 8).map(b => b.toString(16).padStart(2, '0')).join('');
    return `${file.size}:${file.lastModified}:${shortHash}`;
  };

  const queueFilesForIngest = async (e) => {
    const selectedFiles = Array.from(e.target.files || []);
    if (fileInputRef.current) fileInputRef.current.value = '';
    if (selectedFiles.length === 0) return;

    // Always do a fresh fetch before checking duplicates — avoids stale-state false positives
    setUploadStatus({ type: 'info', text: 'Checking for duplicates…' });
    const freshFiles = await fetchFiles();
    const currentFiles = freshFiles ?? files;

    const existingNames = new Set([
      ...currentFiles.map(f => (f.name || '').toLowerCase()),
      ...pendingUploads.map(n => n.toLowerCase()),
    ]);

    // Duplicate within selection (same name picked twice)
    const nameCounts = {};
    for (const f of selectedFiles) {
      const k = (f.name || '').toLowerCase();
      nameCounts[k] = (nameCounts[k] || 0) + 1;
    }
    if (Object.values(nameCounts).some(v => v > 1)) {
      setUploadStatus({ type: 'error', text: 'Duplicate filename in selection. Remove duplicates before ingesting.' });
      return;
    }

    // Split: files that already exist vs files that are new
    const alreadyExists = selectedFiles.filter(f => existingNames.has((f.name || '').toLowerCase()));
    const newFiles      = selectedFiles.filter(f => !existingNames.has((f.name || '').toLowerCase()));

    // Lightweight within-selection duplicate check for batch speed.
    const hashes = await Promise.all(newFiles.map(async (f) => {
      try { return await quickFingerprint(f); }
      catch { return ''; }
    }));
    const seenH = new Set();
    const contentDupes = [];
    const toQueue = [];
    for (let i = 0; i < newFiles.length; i++) {
      const h = hashes[i];
      if (h && seenH.has(h)) {
        contentDupes.push(newFiles[i].name);
      } else {
        if (h) seenH.add(h);
        toQueue.push(newFiles[i]);
      }
    }

    // Nothing left to ingest
    if (toQueue.length === 0) {
      const parts = [];
      if (alreadyExists.length) parts.push(`${alreadyExists.map(f => `"${f.name}"`).join(', ')} already exist`);
      if (contentDupes.length)  parts.push(`${contentDupes.length} identical file(s) skipped`);
      setUploadStatus({ type: 'error', text: parts.join('. ') + '.' });
      return;
    }

    // Some skipped, rest can proceed — show a warning then open the ingest panel
    const warnings = [];
    if (alreadyExists.length) warnings.push(`Skipping ${alreadyExists.map(f => `"${f.name}"`).join(', ')} — already in knowledge base.`);
    if (contentDupes.length)  warnings.push(`Skipping ${contentDupes.length} identical file(s).`);

    setQueuedFiles(toQueue);
    setShowIngestPanel(true);
    setUploadStatus(
      warnings.length
        ? { type: 'info', text: `${warnings.join(' ')} Ingesting ${toQueue.length} new file${toQueue.length > 1 ? 's' : ''}.` }
        : { type: 'info', text: `${toQueue.length} file${toQueue.length > 1 ? 's' : ''} ready to ingest.` }
    );
  };

  const handleIngestQueuedFiles = async () => {
    if (queuedFiles.length === 0) return;
    setIsUploading(true);
    setUploadProgress(0);
    setShowIngestPanel(false);

    const queued = [];
    const uploadErrors = [];
    const total = queuedFiles.length;

    const uploadPctByFile = {};
    const updateAggregateProgress = () => {
      const totalPct = queuedFiles.reduce((sum, f) => sum + (uploadPctByFile[f.name] || 0), 0);
      const aggregate = total > 0 ? totalPct / total : 0;
      safeSetProgress(aggregate * 55);
    };

    const maxConcurrentUploads = Math.max(2, Math.min(6, total >= 6 ? 5 : 3));
    let nextIndex = 0;

    const uploadOne = async (fileObj, index) => {
      setUploadPhaseLabel(`Uploading ${fileObj.name}${total > 1 ? ` (${index + 1} of ${total})` : ''}…`);
      const formData = new FormData();
      formData.append('file', fileObj);
      formData.append('upload_context', uploadContext || '');
      try {
        await axios.post('/api/upload', formData, {
          headers: { 'Content-Type': 'multipart/form-data' },
          onUploadProgress: (evt) => {
            const pct = evt.total ? (evt.loaded / evt.total) : 0;
            uploadPctByFile[fileObj.name] = Math.max(0, Math.min(1, pct));
            updateAggregateProgress();
          },
        });
        uploadPctByFile[fileObj.name] = 1;
        queued.push(fileObj.name);
      } catch (err) {
        uploadPctByFile[fileObj.name] = 1;
        const msg =
          err?.response?.data?.detail ||
          err?.response?.data?.message ||
          err?.message ||
          'Unknown upload error';
        uploadErrors.push(`"${fileObj.name}": ${msg}`);
      } finally {
        updateAggregateProgress();
      }
    };

    const worker = async () => {
      while (true) {
        const current = nextIndex;
        nextIndex += 1;
        if (current >= total) break;
        await uploadOne(queuedFiles[current], current);
      }
    };

    await Promise.all(
      Array.from({ length: Math.min(maxConcurrentUploads, total) }, () => worker())
    );

    if (queued.length > 0) {
      setPendingUploads(prev => Array.from(new Set([...prev, ...queued])));
      setUploadPhaseLabel(`Processing ${queued.length} file${queued.length > 1 ? 's' : ''}…`);
      safeSetProgress(60);
      // Show upload errors as a warning alongside the processing state
      setUploadStatus(
        uploadErrors.length
          ? { type: 'error', text: `${uploadErrors.length} file(s) failed to upload: ${uploadErrors.join(' · ')}` }
          : null
      );
    }

    await fetchFiles();
    setIsUploading(false);
    setQueuedFiles([]);
    setUploadContext('');

    if (queued.length === 0) {
      setUploadPhaseLabel('');
      setUploadStatus({ type: 'error', text: uploadErrors.join(' · ') });
      setTimeout(() => { setUploadProgress(0); setUploadStatus(null); }, 6000);
    }
  };

  const getIcon = (type) => {
    if (type === 'video') return <FileVideo size={14} className="text-accent-main" />;
    if (type === 'image') return <FileImage size={14} className="text-accent-main" />;
    return <FileText size={14} className="text-accent-main" />;
  };

  const isBrainDumpFile = (file) => (
    file?.is_brain_dump === true
    || !!file?.note_id
    || NOTE_FILENAME_RE.test(String(file?.name || '').trim())
    || String(file?.name || '').toLowerCase().startsWith('brain_dump_')
  );

  const brainDumpFiles = files
    .filter(isBrainDumpFile)
    .sort((a, b) => {
      const ai = Number.isFinite(a?.brain_dump_index) ? a.brain_dump_index : -1;
      const bi = Number.isFinite(b?.brain_dump_index) ? b.brain_dump_index : -1;
      if (ai !== bi) return bi - ai;
      return String(a.display_name || a.name || '').localeCompare(String(b.display_name || b.name || ''), undefined, {
        numeric: true,
        sensitivity: 'base',
      });
    });

  const regularFiles = files
    .filter(f => !isBrainDumpFile(f))
    .sort((a, b) => String(a.name || '').localeCompare(String(b.name || '')));

  const renderFileRow = (file) => (
    <div key={file.name} className="flex items-center justify-between p-3 rounded-xl bg-white/0 hover:bg-white/5 transition-all group border border-transparent hover:border-white/5">
      <button
        onClick={() => {
          if (isBrainDumpFile(file) && onOpenBrainDumpNote) {
            onOpenBrainDumpNote(file.note_id || null, file.name || null);
            return;
          }
          if (onPreview) onPreview(file.name);
        }}
        className="flex-1 flex items-center gap-3 overflow-hidden text-gray-400 group-hover:text-white transition-colors"
      >
        <div className="p-1.5 bg-white/5 rounded-lg">{getIcon(file.type)}</div>
        <span className="text-[13px] font-medium truncate text-left">{file.display_name || file.name}</span>
      </button>
      <button
        onClick={() => handleDelete(file.name)}
        className="p-2 text-gray-500 hover:text-red-400 transition-all opacity-90"
        title="Delete file completely"
        aria-label={`Delete ${file.name}`}
      >
        <Trash2 size={14} />
      </button>
    </div>
  );

  const showProgress = isUploading || pendingUploads.length > 0 || uploadProgress > 0;

  return (
    <div className="flex flex-col h-full glass-panel rounded-3xl overflow-hidden font-sans animate-spring-pop shadow-2xl">
      <div className="px-6 py-5 border-b border-white/5 flex items-center justify-between">
        <h2 className="flex items-center gap-3 text-sm font-bold uppercase tracking-widest text-white/90">
          <Database size={16} className="text-accent-main" />
          Knowledge Base
        </h2>
        <span className="text-[10px] bg-white/5 border border-white/5 px-2 py-0.5 rounded-full text-gray-500 font-bold tracking-tighter">
          {files.length} FILES
        </span>
      </div>

      <div className="p-4">
        <input type="file" multiple ref={fileInputRef} className="hidden" onChange={queueFilesForIngest} />
        <button
          onClick={handleUploadClick}
          disabled={isUploading}
          className="w-full flex items-center justify-center gap-3 p-4 rounded-2xl bg-white/5 hover:bg-white/10 border border-white/5 transition-all group disabled:opacity-50"
        >
          {isUploading
            ? <Loader2 className="animate-spin text-accent-main" size={18} />
            : <UploadCloud size={18} className="text-accent-main group-hover:scale-110 transition-transform" />}
          <span className="text-[13px] font-semibold text-gray-300">
            {isUploading ? 'Uploading…' : 'Upload Resources'}
          </span>
        </button>

        <p className="mt-2 text-center text-[10px] text-gray-500 font-medium">
          Supports: PDF, DOCX, TXT, MD, JPG, PNG, WEBP, MP4, MOV, AVI, MKV
        </p>

        {/* Ingest panel */}
        {showIngestPanel && (
          <div className="mt-3 p-3 rounded-xl bg-white/5 border border-white/10 space-y-2">
            <p className="text-[11px] text-gray-300 font-semibold">
              Ingest {queuedFiles.length} file{queuedFiles.length > 1 ? 's' : ''}
            </p>
            <textarea
              value={uploadContext}
              onChange={e => setUploadContext(e.target.value)}
              placeholder="Optional context for this batch…"
              className="w-full h-20 resize-none rounded-lg bg-black/30 border border-white/10 text-[11px] text-gray-200 p-2 focus:outline-none focus:border-accent-main/40"
            />
            <div className="flex justify-end gap-2">
              <button
                onClick={() => { setQueuedFiles([]); setUploadContext(''); setShowIngestPanel(false); setUploadStatus(null); }}
                className="px-3 py-1.5 text-[11px] rounded-lg bg-white/10 hover:bg-white/20 text-gray-200 transition-all"
              >Cancel</button>
              <button
                onClick={handleIngestQueuedFiles}
                disabled={isUploading}
                className="px-3 py-1.5 text-[11px] rounded-lg bg-accent-main hover:brightness-110 text-white transition-all disabled:opacity-50"
              >Ingest Files</button>
            </div>
          </div>
        )}

        {/* Progress + status */}
        {showProgress && (
          <div className="mt-3 space-y-1.5">
            <div className="w-full h-1.5 bg-white/10 rounded-full overflow-hidden">
              <div
                className="h-full bg-accent-main rounded-full transition-all duration-500"
                style={{ width: `${Math.max(4, uploadProgress)}%` }}
              />
            </div>
            {uploadPhaseLabel && (
              <p className="text-[10px] text-gray-400 font-medium truncate">{uploadPhaseLabel}</p>
            )}
          </div>
        )}

        {/* Status message */}
        {uploadStatus && (
          <div className={`mt-2 flex items-start gap-1.5 text-[10px] font-medium
            ${uploadStatus.type === 'error'   ? 'text-red-400'
            : uploadStatus.type === 'success' ? 'text-green-400'
            : 'text-gray-400'}`}>
            {uploadStatus.type === 'error'   && <XCircle size={11} className="shrink-0 mt-0.5" />}
            {uploadStatus.type === 'success' && <CheckCircle size={11} className="shrink-0 mt-0.5" />}
            <span>{uploadStatus.text}</span>
          </div>
        )}
      </div>

      <div className="flex-1 overflow-y-auto px-4 pb-4 space-y-1.5 scrollbar-none">
        {isLoading && (
          <div className="text-center py-10">
            <p className="text-[12px] text-gray-600 font-medium italic">Loading knowledge files…</p>
          </div>
        )}
        {files.length === 0 && !isUploading && !isLoading && (
          <div className="text-center py-10">
            <p className="text-[12px] text-gray-600 font-medium italic">Empty knowledge landscape.</p>
          </div>
        )}
        {brainDumpFiles.length > 0 && (
          <div className="pt-1">
            <p className="px-1 pb-1 text-[10px] uppercase tracking-widest text-gray-500 font-semibold">Brain Dumps</p>
            <div className="space-y-1.5">
              {brainDumpFiles.map(renderFileRow)}
            </div>
          </div>
        )}
        {regularFiles.length > 0 && (
          <div className="pt-1">
            <p className="px-1 pb-1 text-[10px] uppercase tracking-widest text-gray-500 font-semibold">Knowledge Files</p>
            <div className="space-y-1.5">
              {regularFiles.map(renderFileRow)}
            </div>
          </div>
        )}
      </div>

      {deleteTarget && (
        <div className="absolute inset-0 bg-black/55 backdrop-blur-sm flex items-center justify-center z-50 p-4">
          <div className="w-full max-w-md rounded-2xl border border-white/10 bg-[#141419] shadow-2xl p-5">
            <p className="text-sm text-white font-semibold">Delete file permanently?</p>
            <p className="mt-2 text-xs text-gray-400 break-all">{deleteTarget}</p>
            <p className="mt-2 text-[11px] text-gray-500">This removes it from knowledge base, map, and local storage.</p>
            <div className="mt-4 flex items-center justify-end gap-2">
              <button onClick={() => setDeleteTarget(null)}
                className="px-3 py-1.5 text-[11px] rounded-lg bg-white/10 hover:bg-white/20 text-gray-200 transition-all">
                Cancel
              </button>
              <button onClick={confirmDelete}
                className="px-3 py-1.5 text-[11px] rounded-lg bg-red-500/80 hover:bg-red-500 text-white transition-all">
                Confirm Delete
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
