import { useState, useRef, useEffect } from 'react';
import { Send, Loader2, BrainCircuit, Sparkles, ChevronRight, Quote, Play, ExternalLink } from 'lucide-react';
import ReactMarkdown from 'react-markdown';

function VideoClipPlayer({ href, label }) {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const displayLabel = typeof label === 'string' && label && label !== href ? label : 'Video Clip';
  return (
    <div className="my-3 rounded-2xl overflow-hidden bg-white/5 border border-white/10 shadow-lg">
      <div className="px-3 py-2 flex items-center gap-2 border-b border-white/5">
        <Play size={11} className="text-accent-main shrink-0" />
        <span className="text-[11px] font-semibold text-gray-300 truncate">{displayLabel}</span>
        <a href={href} target="_blank" rel="noopener noreferrer"
           className="ml-auto flex items-center gap-1 text-[10px] text-gray-500 hover:text-gray-300 transition-colors shrink-0">
          <ExternalLink size={9} />open
        </a>
      </div>
      {error ? (
        <div className="px-4 py-3 text-[12px] text-gray-400 italic">
          Could not load clip.{' '}
          <a href={href} target="_blank" rel="noopener noreferrer"
             className="text-accent-main/80 hover:text-accent-main underline">
            Open in browser
          </a>
        </div>
      ) : (
        <div className="relative bg-black">
          {loading && (
            <div className="absolute inset-0 flex items-center justify-center z-10">
              <Loader2 size={20} className="text-accent-main animate-spin" />
            </div>
          )}
          <video
            controls
            preload="metadata"
            className="w-full max-h-52 block"
            src={href}
            onCanPlay={() => setLoading(false)}
            onError={() => { setLoading(false); setError(true); }}
          />
        </div>
      )}
    </div>
  );
}

export function ChatInterface({ onPreview, messages, setMessages, isThinking, setIsThinking, agentName, onSendRequest, chatProvider = 'gemini' }) {
  const [input, setInput] = useState('');
  const [isStreaming, setIsStreaming] = useState(false);
  const endOfMessagesRef = useRef(null);

  useEffect(() => {
    endOfMessagesRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  const handleSend = async (e) => {
    e.preventDefault();
    if (!input.trim() || isStreaming) return;

    if (onSendRequest) onSendRequest();
    const userMsg = input.trim();
    setInput('');
    
    // 1. User Message + Immediate Placeholder
    const msgId = Date.now();
    setMessages(prev => [
      ...prev, 
      { role: 'user', content: userMsg },
      { id: msgId, role: 'assistant', thinking: "", answer: "", citations: [], status: 'thinking' }
    ]);
    setIsStreaming(true);
    setIsThinking(true);

    try {
      const response = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query: userMsg, provider: chatProvider })
      });

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop();

        for (const line of lines) {
          if (line.startsWith('data: ')) {
            try {
              const data = JSON.parse(line.slice(6));
              
              setMessages(prev => prev.map(m => {
                if (m.id === msgId) {
                  if (data.type === 'thinking') {
                    return { ...m, thinking: (m.thinking || "") + data.text, status: 'thinking' };
                  } else if (data.type === 'answer') {
                    setIsThinking(false);
                    return { ...m, answer: (m.answer || "") + data.text, status: 'answering' };
                  } else if (data.type === 'metadata') {
                    setIsThinking(false);
                    return { ...m, citations: data.citations, status: 'complete' };
                  } else if (data.type === 'error') {
                    setIsThinking(false);
                    return { ...m, answer: data.text, status: 'error' };
                  }
                }
                return m;
              }));
            } catch (e) {
              console.error("Stream parse error", e);
            }
          }
        }
      }
    } catch (err) {
      setMessages(prev => prev.map(m => m.id === msgId ? { ...m, answer: "Error connecting to brain. Ensure the backend is running.", status: 'complete' } : m));
    } finally {
      setIsStreaming(false);
    }
  };

  return (
    <div className="flex flex-col h-full glass-panel rounded-3xl overflow-hidden font-sans animate-spring-pop">
      <div className="px-6 py-4 border-b border-white/5 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <BrainCircuit className="text-accent-main animate-pulse" size={18} />
          <h2 className="text-sm font-semibold tracking-tight text-white/90">{agentName}</h2>
        </div>
        {isStreaming && <Sparkles size={14} className="text-accent-main animate-bounce" />}
      </div>
      
      <div className="flex-1 overflow-y-auto p-5 space-y-8 scrollbar-none scroll-smooth">
        {messages.map((msg, i) => (
          <div key={i} className={`flex flex-col ${msg.role === 'user' ? 'items-end' : 'items-start'} animate-in fade-in duration-500`}>
            {msg.role === 'user' ? (
              <div className="max-w-[85%] bg-accent-main text-white px-5 py-2.5 rounded-2xl rounded-tr-sm text-[14.5px] font-medium shadow-lg">
                {msg.content}
              </div>
            ) : (
              <div className="w-full space-y-4 max-w-full overflow-hidden">
                {/* Thinking Process Accordion */}
                {msg.thinking && (
                  <details open={msg.status === 'thinking'} className="group w-full">
                    <summary className="flex items-center gap-2 text-[11px] font-bold text-gray-500 uppercase tracking-widest cursor-pointer hover:text-gray-400 transition-colors list-none">
                      <ChevronRight size={14} className="group-open:rotate-90 transition-transform text-accent-main" />
                      Thinking Process
                      {msg.status === 'thinking' && <Loader2 size={12} className="animate-spin ml-auto" />}
                    </summary>
                    <div className="mt-3 p-4 bg-white/5 border border-white/5 rounded-2xl text-[13px] leading-relaxed text-gray-400 font-normal italic whitespace-pre-wrap animate-in fade-in duration-500">
                      {msg.thinking}
                    </div>
                  </details>
                )}

                {/* Main Response: Robust wrapping, no horizontal scroll */}
                <div className={`text-[15px] leading-[1.8] font-normal font-sans tracking-tight break-words whitespace-pre-wrap prose prose-invert max-w-full overflow-hidden ${msg.status === 'error' ? 'text-red-400/90 italic' : 'text-gray-200'}`}>
                  <ReactMarkdown
                    components={{
                      a: ({ href, children }) => {
                        if (href && href.includes('/api/video-clip')) {
                          return <VideoClipPlayer href={href} label={String(children)} />;
                        }
                        return (
                          <a href={href} target="_blank" rel="noopener noreferrer"
                             className="text-accent-main/80 hover:text-accent-main underline">
                            {children}
                          </a>
                        );
                      }
                    }}
                  >{msg.answer}</ReactMarkdown>
                  {msg.status === 'answering' && (
                     <span className="inline-block w-1.5 h-4 bg-accent-main ml-1 animate-pulse" />
                  )}
                </div>

                {/* Citations Grid */}
                {msg.citations && msg.citations.length > 0 && (
                  <div className="pt-2 flex flex-wrap gap-2 animate-in slide-in-from-bottom-2 duration-500">
                     {msg.citations.map((cite, idx) => (
                        <button 
                          key={idx} 
                          onClick={() => onPreview && onPreview(cite.file, cite.relevant_quote)}
                          className="group flex items-center gap-2 px-3 py-1.5 bg-white/5 hover:bg-white/10 border border-white/5 rounded-full transition-all active:scale-95"
                        >
                          <Quote size={10} className="text-accent-main" />
                          <span className="text-[12px] font-medium text-gray-400 group-hover:text-white truncate max-w-[120px]">{cite.file}</span>
                          <div className="w-[1px] h-3 bg-white/10" />
                          <span className={`text-[10px] font-bold ${cite.confidence > 80 ? 'text-green-500/80' : 'text-yellow-500/80'}`}>
                            {cite.confidence}%
                          </span>
                        </button>
                     ))}
                  </div>
                )}
              </div>
            )}
          </div>
        ))}
        <div ref={endOfMessagesRef} />
      </div>

      <form onSubmit={handleSend} className="p-4 bg-dark-900/50 border-t border-white/5">
        <div className="relative flex items-center bg-white/5 border border-white/10 rounded-2xl focus-within:border-accent-main/30 transition-all shadow-inner">
          <input
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="Query your second brain..."
            className="flex-1 p-4 bg-transparent text-white focus:outline-none text-[14.5px] placeholder:text-gray-600 font-sans"
            disabled={isStreaming}
          />
          <button 
            type="submit" 
            disabled={isStreaming}
            className="p-2.5 m-1.5 bg-accent-main text-white rounded-xl hover:brightness-110 active:scale-90 transition-all shadow-lg disabled:opacity-30 disabled:grayscale flex items-center justify-center animate-in zoom-in-90 duration-300"
          >
            <Send size={18} />
          </button>
        </div>
      </form>
    </div>
  );
}
