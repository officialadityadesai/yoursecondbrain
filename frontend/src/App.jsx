import { useState, useEffect } from 'react';
import { Database, MessageSquare } from 'lucide-react';
import { ChatInterface } from './components/ChatInterface';
import { FileManager } from './components/FileManager';
import { KnowledgeGraph } from './components/KnowledgeGraph';
import { PreviewModal } from './components/PreviewModal';

function App() {
  const [activePanes, setActivePanes] = useState({ chat: false, files: false });
  const [dataVersion, setDataVersion] = useState(0);
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

  return (
    <div className="h-screen w-screen relative bg-[#0a0a0a] text-gray-200 overflow-hidden font-sans" style={{ fontFamily: 'var(--font-sans), sans-serif' }}>
      
      {/* Background Spatial Canvas (Always Interactive) */}
      <div className={`absolute inset-0 transition-all duration-700 ease-out ${activePanes.chat || activePanes.files ? 'opacity-60' : 'opacity-100'}`}>
        <KnowledgeGraph
          onPreview={handlePreview}
          showNodeLabels={showNodeLabels}
          onShowNodeLabelsChange={setShowNodeLabels}
          agentName={agentName}
          onAgentNameChange={setAgentName}
          refreshKey={dataVersion}
          onDataChanged={handleDataChanged}
          onOpenFiles={() => togglePane('files')}
        />
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
           <FileManager onPreview={handlePreview} onDataChanged={handleDataChanged} />
        </div>
      )}

      {activePanes.chat && (
        <div className="absolute right-6 top-1/2 -translate-y-1/2 h-[75vh] w-[420px] z-30 pointer-events-auto">
           <ChatInterface 
             onPreview={handlePreview} 
             messages={messages} 
               setMessages={setMessages}
               isThinking={isThinking}
               setIsThinking={setIsThinking}
               agentName={agentName}
               chatProvider="gemini"
              />
        </div>
      )}

      {/* Preview Modal: Topmost Layer */}
      {previewData && (
        <PreviewModal 
          nodeId={previewData.nodeId} 
          highlight={previewData.highlight} 
          onClose={() => setPreviewData(null)} 
          onPreview={handlePreview}
        />
      )}

    </div>
  );
}

export default App;
