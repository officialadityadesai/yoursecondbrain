import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  BrainCircuit,
  Loader2,
  Plus,
  Save,
  X,
  Trash2,
  Bold,
  Italic,
  List,
  Heading,
  Quote,
} from 'lucide-react';
import axios from 'axios';
import TurndownService from 'turndown';
import { marked } from 'marked';

const NEW_NOTE_DRAFT = {
  note_id: null,
  title: '',
  content: '',
  context: '',
  needs_indexing: false,
  index_status: null,
  source_file: null,
};

function formatTimestamp(ts) {
  if (!ts) return 'Never';
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return 'Never';
  return d.toLocaleString();
}

export function BrainDumpWorkspace({ isOpen, onClose, onDataChanged, initialNoteId = null, initialSourceFile = null }) {
  const [notes, setNotes] = useState([]);
  const [isLoading, setIsLoading] = useState(false);
  const [selectedNoteId, setSelectedNoteId] = useState(null);
  const [draft, setDraft] = useState(NEW_NOTE_DRAFT);
  const [isSaving, setIsSaving] = useState(false);
  const [isDeleting, setIsDeleting] = useState(false);
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
  const [status, setStatus] = useState(null); // { type: 'info' | 'success' | 'error', text: string }

  const editorRef = useRef(null);
  const autosaveTimerRef = useRef(null);
  const pauseAutosaveRef = useRef(false);
  const lastSavedSignatureRef = useRef('');
  const hasInitializedRef = useRef(false);
  const selectedNoteIdRef = useRef(null);
  const lastHandledInitialNoteIdRef = useRef(null);
  const lastHandledInitialSourceFileRef = useRef(null);

  useEffect(() => {
    selectedNoteIdRef.current = selectedNoteId;
  }, [selectedNoteId]);

  const turndownService = useMemo(() => {
    const service = new TurndownService({
      headingStyle: 'atx',
      codeBlockStyle: 'fenced',
      bulletListMarker: '-',
    });
    return service;
  }, []);

  useEffect(() => {
    marked.setOptions({ gfm: true, breaks: true });
  }, []);

  const markdownToHtml = useCallback((markdown) => {
    const rendered = marked.parse(markdown || '');
    if (typeof rendered !== 'string') return '<p><br></p>';
    return rendered.trim() ? rendered : '<p><br></p>';
  }, []);

  const htmlToMarkdown = useCallback((html) => {
    const raw = String(html || '').trim();
    if (!raw || raw === '<br>' || raw === '<p><br></p>') return '';
    const md = turndownService.turndown(raw).trim();
    return md;
  }, [turndownService]);

  const setEditorContent = useCallback((markdown) => {
    if (!editorRef.current) return;
    editorRef.current.innerHTML = markdownToHtml(markdown);
  }, [markdownToHtml]);

  const syncDraftFromEditor = useCallback(() => {
    if (!editorRef.current) return;
    const markdown = htmlToMarkdown(editorRef.current.innerHTML);
    setDraft((prev) => ({ ...prev, content: markdown }));
  }, [htmlToMarkdown]);

  const safeSetStatus = useCallback((next) => {
    setStatus(next);
    if (!next || next.type === 'error') return;
    setTimeout(() => {
      setStatus((prev) => (prev?.text === next.text ? null : prev));
    }, 3200);
  }, []);

  const noteSignature = useMemo(() => {
    return JSON.stringify({
      note_id: draft.note_id || null,
      title: draft.title,
      content: draft.content,
      context: draft.context,
    });
  }, [draft.note_id, draft.title, draft.content, draft.context]);

  const hydrateDraft = useCallback((note) => {
    const next = {
      note_id: note.note_id,
      title: note.title || '',
      content: note.content || '',
      context: note.context || '',
      needs_indexing: !!note.needs_indexing,
      index_status: note.index_status || null,
      source_file: note.source_file || null,
    };
    pauseAutosaveRef.current = true;
    setDraft(next);
    setTimeout(() => {
      setEditorContent(next.content);
      pauseAutosaveRef.current = false;
    }, 0);

    lastSavedSignatureRef.current = JSON.stringify({
      note_id: next.note_id || null,
      title: next.title,
      content: next.content,
      context: next.context,
    });
  }, [setEditorContent]);

  const loadNotes = useCallback(async (preferredNoteId = null, preferredSourceFile = null, showSpinner = true) => {
    if (showSpinner) setIsLoading(true);
    try {
      const res = await axios.get('/api/notes');
      const list = (res.data?.notes || []).sort((a, b) => String(b.updated_at || '').localeCompare(String(a.updated_at || '')));
      setNotes(list);

      const chosenById = preferredNoteId && list.find((n) => n.note_id === preferredNoteId);
      const preferredSource = String(preferredSourceFile || '').toLowerCase();
      const chosenBySource = !chosenById && preferredSource
        ? list.find((n) => String(n.source_file || '').toLowerCase() === preferredSource)
        : null;
      const chosen = chosenById || chosenBySource;

      if (chosen) {
        setSelectedNoteId(chosen.note_id);
      } else if (!selectedNoteIdRef.current || !list.some((n) => n.note_id === selectedNoteIdRef.current)) {
        setSelectedNoteId(list[0]?.note_id || null);
      }
      return list;
    } catch (err) {
      safeSetStatus({ type: 'error', text: err?.response?.data?.detail || 'Could not load your notes.' });
      return [];
    } finally {
      if (showSpinner) setIsLoading(false);
    }
  }, [safeSetStatus]);

  const loadNote = useCallback(async (noteId) => {
    if (!noteId) return;
    try {
      const res = await axios.get(`/api/notes/${encodeURIComponent(noteId)}`);
      if (selectedNoteIdRef.current !== noteId) return;
      hydrateDraft(res.data);
    } catch (err) {
      safeSetStatus({ type: 'error', text: err?.response?.data?.detail || 'Could not open this note.' });
    }
  }, [hydrateDraft, safeSetStatus]);

  const waitForSaveCompletion = useCallback(async (sourceFile) => {
    if (!sourceFile) return;
    for (let i = 0; i < 80; i += 1) {
      try {
        const res = await axios.get(`/api/upload-status/${encodeURIComponent(sourceFile)}`);
        const current = res.data?.status || 'unknown';

        if (current === 'queued') {
          safeSetStatus({ type: 'info', text: 'Preparing your note...' });
        } else if (current === 'processing') {
          safeSetStatus({ type: 'info', text: 'Saving to your knowledge base...' });
        } else if (current === 'done') {
          safeSetStatus({ type: 'success', text: 'Saved.' });
          if (onDataChanged) onDataChanged();
          return;
        } else if (current === 'failed') {
          safeSetStatus({ type: 'error', text: res.data?.error || 'Save failed. Please try again.' });
          return;
        }
      } catch {
        // Continue polling until timeout.
      }
      await new Promise((resolve) => setTimeout(resolve, 1500));
    }
    safeSetStatus({ type: 'error', text: 'Save took too long. Please try again.' });
  }, [onDataChanged, safeSetStatus]);

  const persistNote = useCallback(async (saveToKnowledgeBase, silent = false) => {
    if (isSaving || isDeleting) return null;

    const title = (draft.title || '').trim();
    if (!title) {
      if (!silent) safeSetStatus({ type: 'error', text: 'Add a title first.' });
      return null;
    }

    const liveContent = editorRef.current ? htmlToMarkdown(editorRef.current.innerHTML) : draft.content;
    if (saveToKnowledgeBase && !liveContent.trim()) {
      if (!silent) safeSetStatus({ type: 'error', text: 'Write something before saving.' });
      return null;
    }

    if (!silent) {
      safeSetStatus({ type: 'info', text: saveToKnowledgeBase ? 'Saving note...' : 'Saving changes...' });
    }
    setIsSaving(true);

    try {
      const payload = {
        note_id: draft.note_id,
        title,
        content: liveContent,
        context: draft.context,
        index_now: saveToKnowledgeBase,
      };
      const res = await axios.post('/api/notes/save', payload);
      const note = res.data?.note;
      const indexState = res.data?.index_state;

      if (!note) {
        throw new Error('Missing note data from server.');
      }

      setDraft((prev) => ({
        ...prev,
        note_id: note.note_id,
        title: note.title || title,
        content: liveContent,
        context: note.context ?? prev.context,
        needs_indexing: !!note.needs_indexing,
        index_status: note.index_status || null,
        source_file: note.source_file || prev.source_file,
      }));

      lastSavedSignatureRef.current = JSON.stringify({
        note_id: note.note_id || null,
        title: note.title || title,
        content: liveContent,
        context: note.context ?? draft.context,
      });

      setSelectedNoteId(note.note_id);
      if (silent) {
        setNotes((prev) => {
          if (!prev.length) return prev;
          let found = false;
          const updated = prev.map((item) => {
            if (item.note_id !== note.note_id) return item;
            found = true;
            return { ...item, ...note };
          });
          return found ? updated : [note, ...updated];
        });
      } else {
        await loadNotes(note.note_id, null, false);
      }

      if (saveToKnowledgeBase) {
        if (indexState === 'queued') {
          await waitForSaveCompletion(note.source_file);
        } else {
          safeSetStatus({ type: 'success', text: 'Saved.' });
          if (onDataChanged) onDataChanged();
        }
      } else if (!silent) {
        safeSetStatus({ type: 'success', text: 'Saved.' });
      }

      return note;
    } catch (err) {
      const detail = err?.response?.data?.detail || err?.message || 'Save failed.';
      safeSetStatus({ type: 'error', text: detail });
      return null;
    } finally {
      setIsSaving(false);
    }
  }, [draft, htmlToMarkdown, isDeleting, isSaving, loadNotes, onDataChanged, safeSetStatus, waitForSaveCompletion]);

  const createBlankNote = useCallback(async () => {
    if (isSaving || isDeleting) return;

    const existingTitles = new Set(notes.map((n) => String(n.title || '').trim().toLowerCase()));
    const baseTitle = 'Untitled Note';
    let nextTitle = baseTitle;
    let suffix = 2;
    while (existingTitles.has(nextTitle.toLowerCase())) {
      nextTitle = `${baseTitle} ${suffix}`;
      suffix += 1;
    }

    setIsSaving(true);
    safeSetStatus({ type: 'info', text: 'Creating a new note...' });

    try {
      const res = await axios.post('/api/notes/save', {
        note_id: null,
        title: nextTitle,
        content: '',
        context: '',
        index_now: false,
      });
      const note = res.data?.note;
      if (!note) throw new Error('Missing note data from server.');

      hydrateDraft(note);
      setSelectedNoteId(note.note_id);
      await loadNotes(note.note_id, null, false);
      safeSetStatus({ type: 'success', text: 'New note ready.' });
    } catch (err) {
      const detail = err?.response?.data?.detail || err?.message || 'Could not create a new note.';
      safeSetStatus({ type: 'error', text: detail });
    } finally {
      setIsSaving(false);
    }
  }, [hydrateDraft, isDeleting, isSaving, loadNotes, notes, safeSetStatus]);

  const handleNewNote = () => {
    void createBlankNote();
  };

  const handleDeleteCurrent = () => {
    if (!draft.note_id) return;
    setShowDeleteConfirm(true);
  };

  const confirmDeleteCurrent = async () => {
    if (!draft.note_id || isDeleting) return;

    setIsDeleting(true);
    try {
      await axios.delete(`/api/notes/${encodeURIComponent(draft.note_id)}`);
      setShowDeleteConfirm(false);
      safeSetStatus({ type: 'success', text: 'Note deleted.' });
      if (onDataChanged) onDataChanged();

      const list = await loadNotes();
      if (!list.length) {
        await createBlankNote();
      }
    } catch (err) {
      safeSetStatus({ type: 'error', text: err?.response?.data?.detail || 'Delete failed.' });
    } finally {
      setIsDeleting(false);
    }
  };

  const applyCommand = useCallback((command, value = null) => {
    if (!editorRef.current) return;
    editorRef.current.focus({ preventScroll: true });
    if (value === null) {
      document.execCommand(command, false);
    } else {
      document.execCommand(command, false, value);
    }
    syncDraftFromEditor();
  }, [syncDraftFromEditor]);

  const keepSelection = useCallback((event) => {
    event.preventDefault();
  }, []);

  const toggleHeadingBlock = useCallback(() => {
    if (!editorRef.current) return;

    const selection = window.getSelection();
    let currentNode = selection?.anchorNode || null;

    if (currentNode?.nodeType === 3) {
      currentNode = currentNode.parentElement;
    }

    const currentElement = currentNode && currentNode.nodeType === 1 ? currentNode : null;
    const activeHeading = currentElement?.closest?.('h1, h2, h3, h4, h5, h6');
    const isInsideHeading = !!(activeHeading && editorRef.current.contains(activeHeading));

    editorRef.current.focus({ preventScroll: true });
    document.execCommand('formatBlock', false, isInsideHeading ? 'p' : 'h2');
    syncDraftFromEditor();
  }, [syncDraftFromEditor]);

  const toggleQuoteBlock = useCallback(() => {
    if (!editorRef.current) return;

    const selection = window.getSelection();
    let currentNode = selection?.anchorNode || null;

    if (currentNode?.nodeType === 3) {
      currentNode = currentNode.parentElement;
    }

    const currentElement = currentNode && currentNode.nodeType === 1 ? currentNode : null;
    const activeQuote = currentElement?.closest?.('blockquote');
    const isInsideQuote = !!(activeQuote && editorRef.current.contains(activeQuote));

    editorRef.current.focus({ preventScroll: true });
    document.execCommand('formatBlock', false, isInsideQuote ? 'p' : 'blockquote');
    syncDraftFromEditor();
  }, [syncDraftFromEditor]);

  useEffect(() => {
    if (!isOpen) {
      hasInitializedRef.current = false;
      lastHandledInitialNoteIdRef.current = null;
      lastHandledInitialSourceFileRef.current = null;
      return;
    }
    if (hasInitializedRef.current) return;

    hasInitializedRef.current = true;

    const init = async () => {
      const list = await loadNotes(initialNoteId || null, initialSourceFile || null);
      if (!list.length) {
        await createBlankNote();
      }
    };
    void init();
  }, [createBlankNote, initialNoteId, initialSourceFile, isOpen, loadNotes]);

  useEffect(() => {
    if (!isOpen) return;
    if (!initialNoteId) return;
    if (lastHandledInitialNoteIdRef.current === initialNoteId) return;
    lastHandledInitialNoteIdRef.current = initialNoteId;
    setSelectedNoteId(initialNoteId);
  }, [initialNoteId, isOpen]);

  useEffect(() => {
    if (!isOpen) return;
    if (!initialSourceFile || initialNoteId) return;
    if (lastHandledInitialSourceFileRef.current === initialSourceFile) return;
    lastHandledInitialSourceFileRef.current = initialSourceFile;

    const sourceKey = String(initialSourceFile).toLowerCase();
    const matched = notes.find((n) => String(n.source_file || '').toLowerCase() === sourceKey);
    if (matched?.note_id) {
      setSelectedNoteId(matched.note_id);
      return;
    }

    void loadNotes(null, initialSourceFile, false);
  }, [initialNoteId, initialSourceFile, isOpen, loadNotes, notes]);

  useEffect(() => {
    if (!isOpen || !selectedNoteId) return;
    void loadNote(selectedNoteId);
  }, [isOpen, loadNote, selectedNoteId]);

  useEffect(() => {
    if (!isOpen || pauseAutosaveRef.current || !draft.note_id) return undefined;
    if (noteSignature === lastSavedSignatureRef.current) return undefined;

    if (autosaveTimerRef.current) clearTimeout(autosaveTimerRef.current);
    autosaveTimerRef.current = setTimeout(() => {
      void persistNote(false, true);
    }, 1000);

    return () => {
      if (autosaveTimerRef.current) clearTimeout(autosaveTimerRef.current);
    };
  }, [draft.note_id, isOpen, noteSignature, persistNote]);

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-40 bg-black/48 backdrop-blur-sm flex items-center justify-center p-6 pointer-events-auto">
      <div className="w-full max-w-[1360px] h-[88vh] glass-panel rounded-[28px] overflow-hidden shadow-[0_28px_80px_rgba(0,0,0,0.65)] border border-white/10 flex flex-col animate-spring-pop">
        <div className="h-14 px-5 border-b border-white/10 bg-black/25 flex items-center justify-between">
          <div className="flex items-center gap-3 min-w-0">
            <BrainCircuit size={17} className="text-accent-main shrink-0" />
            <div className="min-w-0">
              <p className="text-xs font-semibold text-white/90 tracking-wide">Brain Dump</p>
              <p className="text-[11px] text-gray-400 truncate">Capture ideas quickly. They become searchable knowledge.</p>
            </div>
          </div>

          <div className="flex items-center gap-2">
            <button
              onClick={handleNewNote}
              disabled={isSaving || isDeleting}
              className="px-3 py-1.5 rounded-lg text-[11px] bg-white/10 hover:bg-white/15 text-gray-200 transition-all disabled:opacity-50 flex items-center gap-1.5"
            >
              <Plus size={12} />
              New Note
            </button>
            <button
              onClick={() => persistNote(true, false)}
              disabled={isSaving || isDeleting}
              className="px-3 py-1.5 rounded-lg text-[11px] bg-accent-main hover:brightness-110 text-white transition-all disabled:opacity-50 flex items-center gap-1.5"
            >
              {isSaving ? <Loader2 size={12} className="animate-spin" /> : <Save size={12} />}
              Save Note
            </button>
            <button
              onClick={onClose}
              className="p-2 rounded-lg text-gray-500 hover:text-white hover:bg-white/10 transition-all"
              aria-label="Close workspace"
            >
              <X size={16} />
            </button>
          </div>
        </div>

        <div className="flex-1 min-h-0 flex">
          <aside className="w-[290px] border-r border-white/10 bg-black/20 flex flex-col">
            <div className="p-3 border-b border-white/10 flex items-center gap-2">
              <button
                onClick={handleDeleteCurrent}
                disabled={!draft.note_id || isDeleting}
                className="w-full flex items-center justify-center gap-2 px-3 py-2 rounded-xl bg-red-500/15 hover:bg-red-500/25 text-red-300 text-[11px] font-semibold transition-all disabled:opacity-40"
              >
                <Trash2 size={13} />
                Delete Note
              </button>
            </div>

            <div className="px-3 py-2 text-[11px] uppercase tracking-widest text-gray-500 font-semibold">
              Notes ({notes.length})
            </div>

            <div className="flex-1 overflow-y-auto px-2 pb-3 space-y-1 scrollbar-none">
              {isLoading && notes.length === 0 && (
                <div className="px-3 py-4 text-[11px] text-gray-500 flex items-center gap-2">
                  <Loader2 size={12} className="animate-spin" />
                  Loading notes...
                </div>
              )}

              {!isLoading && notes.length === 0 && (
                <div className="px-3 py-4 text-[11px] text-gray-500 italic">No notes yet.</div>
              )}

              {notes.map((note) => (
                <button
                  key={note.note_id}
                  onClick={() => setSelectedNoteId(note.note_id)}
                  className={`w-full text-left p-2.5 rounded-xl border transition-all ${
                    selectedNoteId === note.note_id
                      ? 'bg-accent-main/20 border-accent-main/40 text-white'
                      : 'bg-white/0 border-transparent hover:bg-white/7 text-gray-300'
                  }`}
                >
                  <p className="text-[13px] font-semibold truncate">{note.title}</p>
                  <p className="mt-0.5 text-[11px] text-gray-500 truncate">
                    {note.needs_indexing ? 'Unsaved changes' : note.indexed_at ? `Saved ${formatTimestamp(note.indexed_at)}` : 'Not saved yet'}
                  </p>
                </button>
              ))}
            </div>
          </aside>

          <main className="flex-1 min-w-0 bg-black/10 flex flex-col">
            <div className="px-10 pt-8 pb-4 border-b border-white/10">
              <input
                value={draft.title}
                onChange={(e) => setDraft((prev) => ({ ...prev, title: e.target.value }))}
                placeholder="Untitled"
                className="w-full bg-transparent text-[48px] leading-tight font-bold text-white placeholder:text-gray-600 focus:outline-none"
              />
            </div>

            <div className="px-10 py-3 border-b border-white/10 flex items-center gap-2 flex-wrap">
              <button onMouseDown={keepSelection} onClick={toggleHeadingBlock} className="px-2 py-1 rounded-md bg-white/10 hover:bg-white/15 text-gray-200" title="Heading"><Heading size={12} /></button>
              <button onMouseDown={keepSelection} onClick={() => applyCommand('bold')} className="px-2 py-1 rounded-md bg-white/10 hover:bg-white/15 text-gray-200" title="Bold"><Bold size={12} /></button>
              <button onMouseDown={keepSelection} onClick={() => applyCommand('italic')} className="px-2 py-1 rounded-md bg-white/10 hover:bg-white/15 text-gray-200" title="Italic"><Italic size={12} /></button>
              <button onMouseDown={keepSelection} onClick={() => applyCommand('insertUnorderedList')} className="px-2 py-1 rounded-md bg-white/10 hover:bg-white/15 text-gray-200" title="Bullet list"><List size={12} /></button>
              <button onMouseDown={keepSelection} onClick={toggleQuoteBlock} className="px-2 py-1 rounded-md bg-white/10 hover:bg-white/15 text-gray-200" title="Quote"><Quote size={12} /></button>
            </div>

            <div className="flex-1 min-h-0 overflow-y-auto px-10 py-8">
              <div
                ref={editorRef}
                contentEditable
                suppressContentEditableWarning
                onInput={syncDraftFromEditor}
                data-placeholder="Start writing..."
                className="note-editor min-h-full text-[18px] md:text-[19px] leading-[1.7] text-gray-200 focus:outline-none"
              />
            </div>
          </main>
        </div>

        <div className="h-9 px-4 border-t border-white/10 bg-black/30 flex items-center justify-between text-[11px] text-gray-500">
          <div className="truncate">
            {draft.needs_indexing ? 'You have unsaved changes.' : 'All changes saved.'}
          </div>
          {status && (
            <div className={`${status.type === 'error' ? 'text-red-400' : status.type === 'success' ? 'text-green-400' : 'text-gray-300'} truncate`}>
              {status.text}
            </div>
          )}
        </div>

        {showDeleteConfirm && (
          <div className="absolute inset-0 bg-black/55 backdrop-blur-sm flex items-center justify-center z-50 p-4">
            <div className="w-full max-w-md rounded-2xl border border-white/10 bg-[#141419] shadow-2xl p-5">
              <p className="text-sm text-white font-semibold">Delete this note permanently?</p>
              <p className="mt-2 text-xs text-gray-400 break-all">{draft.title || 'Untitled Note'}</p>
              <p className="mt-2 text-[11px] text-gray-500">This removes it from your notes and knowledge map.</p>
              <div className="mt-4 flex items-center justify-end gap-2">
                <button
                  onClick={() => setShowDeleteConfirm(false)}
                  disabled={isDeleting}
                  className="px-3 py-1.5 text-[11px] rounded-lg bg-white/10 hover:bg-white/20 text-gray-200 transition-all disabled:opacity-50"
                >
                  Cancel
                </button>
                <button
                  onClick={confirmDeleteCurrent}
                  disabled={isDeleting}
                  className="px-3 py-1.5 text-[11px] rounded-lg bg-red-500/80 hover:bg-red-500 text-white transition-all disabled:opacity-50"
                >
                  {isDeleting ? 'Deleting...' : 'Confirm Delete'}
                </button>
              </div>
            </div>
          </div>
        )}
      </div>

      <style>{`
        .note-editor:empty:before {
          content: attr(data-placeholder);
          color: rgba(156, 163, 175, 0.65);
          pointer-events: none;
        }
        .note-editor {
          font-family: 'Space Grotesk', 'Inter', system-ui, sans-serif;
        }
        .note-editor p { margin: 0 0 0.8em; }
        .note-editor h1, .note-editor h2, .note-editor h3 { color: #f3f4f6; margin: 0.7em 0 0.35em; line-height: 1.2; }
        .note-editor h1 { font-size: 1.6em; font-weight: 700; }
        .note-editor h2 { font-size: 1.3em; font-weight: 700; }
        .note-editor h3 { font-size: 1.15em; font-weight: 600; }
        .note-editor ul, .note-editor ol { margin: 0.6em 0 0.9em 1.4em; }
        .note-editor ul { list-style-type: disc; }
        .note-editor ol { list-style-type: decimal; }
        .note-editor ul, .note-editor ol { list-style-position: outside; }
        .note-editor li { margin: 0.3em 0; }
        .note-editor blockquote {
          border-left: 3px solid rgba(10, 132, 255, 0.55);
          padding-left: 0.8em;
          color: rgba(229, 231, 235, 0.88);
          margin: 0.8em 0;
        }
        .note-editor code {
          background: rgba(255, 255, 255, 0.08);
          border-radius: 6px;
          padding: 0.08em 0.35em;
          font-size: 0.85em;
        }
        .note-editor pre {
          background: rgba(0, 0, 0, 0.35);
          border: 1px solid rgba(255, 255, 255, 0.1);
          border-radius: 10px;
          padding: 0.8em 1em;
          overflow-x: auto;
        }
      `}</style>
    </div>
  );
}
