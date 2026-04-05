import { lazy, Suspense, useEffect, useState } from 'react';
import { Database, MessageSquare, BrainCircuit } from 'lucide-react';

const lazyNamed = (loader, exportName) =>
  lazy(() => loader().then((mod) => ({ default: mod[exportName] })));

const ChatInterface = lazyNamed(() => import('./components/ChatInterface'), 'ChatInterface');
const FileManager = lazyNamed(() => import('./components/FileManager'), 'FileManager');
const KnowledgeGraph = lazyNamed(() => import('./components/KnowledgeGraph'), 'KnowledgeGraph');
const PreviewModal = lazyNamed(() => import('./components/PreviewModal'), 'PreviewModal');
const BrainDumpWorkspace = lazyNamed(() => import('./components/BrainDumpWorkspace'), 'BrainDumpWorkspace');

function LoadingPanel({ text = 'Loading...' }) {
  return (
    <div className="h-full w-full rounded-3xl border border-white/10 bg-black/40 backdrop-blur-md flex items-center justify-center text-sm text-gray-400">
      {text}
    </div>
  );
}

function App() {
  const [activePanes, setActivePanes] = useState({ chat: false, files: false, brainDump: false });
  const [dataVersion, setDataVersion] = useState(0);
  const [brainDumpInitialNoteId, setBrainDumpInitialNoteId] = useState(null);
  const [brainDumpInitialSourceFile, setBrainDumpInitialSourceFile] = useState(null);
  const [showNodeLabels, setShowNodeLabels] = useState(() => {
    const saved = localStorage.getItem('spatial_showNodeLabels');
    return saved !== null ? JSON.parse(saved) : true;
  });
  const [agentName, setAgentName] = useState(() => localStorage.getItem('spatial_agentName') || 'My Second Brain');
  const [previewData, setPreviewData] = useState(null);
  
  // Persisted Intelligence State
  const [messages, setMessages] = useState([
    { role: 'assistant', text: `Hello. I am ${agentName}. Ask me anything about the knowledge you've stored.`, id: 'initial' }
  ]);
  const [isThinking, setIsThinking] = useState(false);

  // Persistence Sync
  useEffect(() => {
    localStorage.setItem('spatial_showNodeLabels', JSON.stringify(showNodeLabels));
  }, [showNodeLabels]);

  useEffect(() => {
    localStorage.setItem('spatial_agentName', agentName);
  }, [agentName]);

  const togglePane = (pane) => {
    setActivePanes(prev => ({ ...prev, [pane]: !prev[pane] }));
  };

  const handlePreview = (nodeId, highlight = null) => {
    setPreviewData({ nodeId, highlight });
  };

  const handleDataChanged = () => {
    setDataVersion((v) => v + 1);
  };

  const handleOpenBrainDumpNote = (noteId, sourceFile = null) => {
    setPreviewData(null);
    setBrainDumpInitialNoteId(noteId || null);
    setBrainDumpInitialSourceFile(sourceFile || null);
    setActivePanes((prev) => ({ ...prev, brainDump: true }));
  };

  return (
    <div className="h-screen w-screen relative bg-[#0a0a0a] text-gray-200 overflow-hidden font-sans" style={{ fontFamily: 'var(--font-sans), sans-serif' }}>
      
      {/* Background Spatial Canvas (Always Interactive) */}
      <div className={`absolute inset-0 transition-all duration-700 ease-out ${activePanes.chat || activePanes.files || activePanes.brainDump ? 'opacity-60' : 'opacity-100'}`}>
        <Suspense fallback={<LoadingPanel text="Loading knowledge graph..." />}>
          <KnowledgeGraph
            onPreview={handlePreview}
            onOpenBrainDumpNote={handleOpenBrainDumpNote}
            showNodeLabels={showNodeLabels}
            onShowNodeLabelsChange={setShowNodeLabels}
            agentName={agentName}
            onAgentNameChange={setAgentName}
            refreshKey={dataVersion}
            onDataChanged={handleDataChanged}
            onOpenFiles={() => togglePane('files')}
          />
        </Suspense>
      </div>

      {/* Floating Toolbar: Centered Bottom */}
      <div className="absolute bottom-8 left-1/2 -translate-x-1/2 z-40 flex items-center gap-2 p-1.5 glass-panel rounded-2xl shadow-2xl animate-spring-pop hover:scale-[1.02] transition-transform duration-500 pointer-events-auto">
        <button 
          onClick={() => togglePane('files')} 
          className={`flex items-center gap-2 px-6 py-2.5 rounded-xl transition-all duration-300 ${activePanes.files ? 'bg-accent-main text-white shadow-lg' : 'text-gray-400 hover:text-white hover:bg-white/5'}`}
        >
          <Database size={18} />
          <span className="text-[13px] font-semibold tracking-wide">Knowledge Base</span>
        </button>
        <button
          onClick={() => togglePane('brainDump')}
          className={`flex items-center gap-2 px-6 py-2.5 rounded-xl transition-all duration-300 ${activePanes.brainDump ? 'bg-accent-main text-white shadow-lg' : 'text-gray-400 hover:text-white hover:bg-white/5'}`}
        >
          <BrainCircuit size={18} />
          <span className="text-[13px] font-semibold tracking-wide">Brain Dump</span>
        </button>
        <button 
          onClick={() => togglePane('chat')} 
          className={`flex items-center gap-2 px-6 py-2.5 rounded-xl transition-all duration-300 ${activePanes.chat ? 'bg-accent-main text-white shadow-lg' : 'text-gray-400 hover:text-white hover:bg-white/5'}`}
        >
          <MessageSquare size={18} />
          <span className="text-[13px] font-semibold tracking-wide">Agent</span>
        </button>
      </div>


      {/* Floating Panes: Slide Transitions */}
      {activePanes.files && (
        <div className="absolute left-6 top-1/2 -translate-y-1/2 h-[75vh] w-[380px] z-30 pointer-events-auto">
          <Suspense fallback={<LoadingPanel text="Loading file manager..." />}>
            <FileManager onPreview={handlePreview} onDataChanged={handleDataChanged} onOpenBrainDumpNote={handleOpenBrainDumpNote} refreshKey={dataVersion} />
          </Suspense>
        </div>
      )}

      {activePanes.chat && (
        <div className="absolute right-6 top-1/2 -translate-y-1/2 h-[75vh] w-[420px] z-30 pointer-events-auto">
          <Suspense fallback={<LoadingPanel text="Loading chat..." />}>
            <ChatInterface
              onPreview={handlePreview}
              messages={messages}
               setMessages={setMessages}
               isThinking={isThinking}
               setIsThinking={setIsThinking}
               agentName={agentName}
               chatProvider="gemini"
            />
          </Suspense>
        </div>
      )}

      <Suspense fallback={null}>
        <BrainDumpWorkspace
          isOpen={activePanes.brainDump}
          initialNoteId={brainDumpInitialNoteId}
          initialSourceFile={brainDumpInitialSourceFile}
          onClose={() => {
            setActivePanes(prev => ({ ...prev, brainDump: false }));
            setBrainDumpInitialNoteId(null);
            setBrainDumpInitialSourceFile(null);
          }}
          onDataChanged={handleDataChanged}
        />
      </Suspense>

      {/* Preview Modal: Topmost Layer */}
      {previewData && (
        <Suspense fallback={null}>
          <PreviewModal
            nodeId={previewData.nodeId}
            highlight={previewData.highlight}
            onClose={() => setPreviewData(null)}
            onPreview={handlePreview}
            onRename={(oldName, newName) => {
              // Refresh the graph and file list after rename
              handleDataChanged();
              // If the modal is open on the renamed node, reopen it with new name
              setPreviewData(d => d?.nodeId === oldName ? { ...d, nodeId: newName } : d);
            }}
          />
        </Suspense>
      )}

    </div>
  );
}

export default App;
