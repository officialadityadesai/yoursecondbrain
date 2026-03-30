import { useState, useEffect } from 'react';
import ReactMarkdown from 'react-markdown';
import { X, FileText, Share2, Loader2, Quote, Layout, ChevronRight, Users, Building2, Wrench, Lightbulb, GitBranch } from 'lucide-react';
import axios from 'axios';

function sanitizeContent(content) {
  if (!content) return content;
  // Check ratio of printable ASCII chars in first 2000 chars
  const sample = content.slice(0, 2000);
  let printable = 0;
  for (let i = 0; i < sample.length; i++) {
    const code = sample.charCodeAt(i);
    if ((code >= 0x20 && code <= 0x7E) || code === 0x09 || code === 0x0A || code === 0x0D) {
      printable++;
    }
  }
  const ratio = printable / Math.max(sample.length, 1);
  if (ratio >= 0.85) return content;
  // Extract only printable ASCII regions
  const cleaned = content.replace(/[^\x20-\x7E\t\n\r]+/g, ' ').replace(/ {4,}/g, '\n').trim();
  return cleaned || '_This file contains binary data. Re-upload it to re-index with clean content._';
}

export function PreviewModal({ nodeId, highlight, onClose, onPreview }) {
  const [details, setDetails] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!nodeId) return;
    const fetchDetails = async () => {
      setLoading(true);
      try {
        const res = await axios.get(`/api/nodes/${encodeURIComponent(nodeId)}`);
        setDetails(res.data);
      } catch (err) {
        setDetails({ error: "Node metadata synchronization failed. Ensure indexing is complete." });
      } finally {
        setLoading(false);
      }
    };
    fetchDetails();
  }, [nodeId]);

  if (loading) {
    return (
      <div className="fixed inset-0 z-[100] flex items-center justify-center bg-[#0a0a0a]/40 backdrop-blur-md pointer-events-auto">
        <div className="flex flex-col items-center gap-4 p-8 glass-panel rounded-3xl animate-spring-pop">
           <Loader2 className="animate-spin text-accent-main" size={32} />
           <p className="text-[12px] font-bold text-gray-400 uppercase tracking-widest leading-none">Accessing Node...</p>
        </div>
      </div>
    );
  }

  const isImage = details?.name?.match(/\.(jpeg|jpg|png|gif|webp)$/i);
  const isVideo = details?.source_type === 'video' || details?.name?.match(/\.(mp4|mov|avi|mkv)$/i);
  const isPdf = details?.name?.match(/\.pdf$/i);
  const isDocx = details?.name?.match(/\.docx$/i);
  const encodedFileName = details?.name ? encodeURIComponent(details.name) : "";

  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center p-6 bg-black/60 pointer-events-auto animate-in fade-in duration-500">
      <div className="relative w-full max-w-6xl max-h-[88vh] flex flex-col glass-panel rounded-3xl overflow-hidden animate-spring-pop shadow-[0_0_100px_rgba(0,0,0,0.8)] border border-white/10">
        
        {/* Header: Simplified & Premium */}
        <div className="flex items-center justify-between px-8 py-5 bg-white/5 border-b border-white/5">
          <div className="flex items-center gap-4 truncate">
            <div className="p-2.5 bg-accent-main/10 rounded-xl text-accent-main">
              {details?.type === 'topic' ? <Share2 size={20} /> : <FileText size={20} />}
            </div>
            <div className="truncate">
              <p className="text-[10px] font-bold text-gray-500 uppercase tracking-widest leading-none mb-1.5">Intelligence Node</p>
              <h3 className="font-bold text-xl text-white truncate tracking-tight">{details?.name || "Discovery"}</h3>
            </div>
          </div>
          <button onClick={onClose} className="p-2.5 rounded-xl hover:bg-white/10 text-gray-500 hover:text-white transition-all active:scale-95">
            <X size={20} />
          </button>
        </div>

        {/* Scrollable Content */}
         <div className="flex-1 overflow-y-auto p-8 text-gray-300 bg-dark-950/80 scrollbar-none">
          {details?.error ? (
             <div className="flex flex-col items-center justify-center h-64 gap-4 text-center">
                <Layout size={40} className="text-gray-700" />
                <p className="text-sm text-gray-500 font-medium max-w-xs">{details.error}</p>
             </div>
          ) : (
             <div className="max-w-5xl mx-auto space-y-10">
               
               {/* Visual Assets */}
               {isImage && (
                 <div className="space-y-6 flex flex-col items-center">
                    <img 
                      src={`/brain_data/${encodedFileName}`} 
                      alt={details.name} 
                      className="max-h-[500px] w-auto object-contain rounded-2xl border border-white/10 shadow-2xl scale-100 hover:scale-[1.02] transition-transform duration-700"
                    />
                    <div className="text-center space-y-2">
                       <p className="text-xs text-gray-500 font-bold uppercase tracking-widest">Description</p>
                       <div className="text-gray-400 text-sm italic leading-relaxed max-w-md text-left">
                         <ReactMarkdown>{details.description || "No visual metadata provided."}</ReactMarkdown>
                       </div>
                    </div>
                 </div>
               )}

                 {isPdf && (
                  <div className="flex flex-col h-[78vh] w-full bg-white/5 rounded-2xl border border-white/10 overflow-hidden shadow-2xl">
                     <iframe 
                      src={`/brain_data/${encodedFileName}#view=FitH&zoom=100&pagemode=none&toolbar=1&navpanes=0&scrollbar=1`} 
                      className="w-full h-full border-none"
                      title="PDF Preview"
                    />
                  </div>
                )}

               {isDocx && (
                 <div className="flex flex-col h-[78vh] w-full bg-white rounded-2xl border border-white/10 overflow-hidden shadow-2xl">
                   <iframe
                     src={`/api/docx-preview/${encodedFileName}`}
                     className="w-full h-full border-none"
                     title="Document Preview"
                   />
                 </div>
               )}

               {isVideo && (
                 <div className="space-y-6 flex flex-col items-center">
                    <video 
                      controls
                      src={`/brain_data/${encodedFileName}`} 
                      className="max-h-[500px] w-full max-w-3xl object-contain rounded-2xl border border-white/10 shadow-2xl"
                    />
                    <div className="text-center space-y-2 w-full max-w-2xl">
                       <p className="text-xs text-gray-500 font-bold uppercase tracking-widest">Video Analysis</p>
                       <div className="text-gray-300 text-sm leading-relaxed text-left bg-white/5 p-6 rounded-xl border border-white/5">
                         <ReactMarkdown>{details.content || "No video analysis provided."}</ReactMarkdown>
                       </div>
                    </div>
                 </div>
               )}

               {/* Concepts Section */}
                {details?.topics?.length > 0 && (
                  <div className="space-y-4">
                   <div className="flex items-center gap-2 mb-4">
                     <Share2 size={12} className="text-accent-main" />
                     <h4 className="text-[11px] font-bold text-gray-500 uppercase tracking-widest">Semantic Context</h4>
                   </div>
                   <div className="flex flex-wrap gap-2">
                     {details.topics.map((t, idx) => (
                       <span key={idx} className="px-4 py-1.5 bg-white/5 border border-white/5 rounded-full text-white text-[13px] font-semibold tracking-tight">
                         {t}
                       </span>
                     ))}
                   </div>
                  </div>
                )}

                {details?.upload_context && (
                  <div className="space-y-3">
                    <div className="flex items-center gap-2">
                      <Quote size={12} className="text-accent-main" />
                      <h4 className="text-[11px] font-bold text-gray-500 uppercase tracking-widest">Ingestion Context</h4>
                    </div>
                    <div className="text-[13px] text-gray-300 leading-relaxed bg-white/5 border border-white/10 rounded-xl p-4">
                      {details.upload_context}
                    </div>
                  </div>
                )}

               {/* Entities Section */}
               {(details?.entities?.length > 0 || details?.relationships?.length > 0) && (() => {
                 const byType = {
                   person: details.entities?.filter(e => e.type === 'person') || [],
                   organisation: details.entities?.filter(e => e.type === 'organisation') || [],
                   tool: details.entities?.filter(e => e.type === 'tool') || [],
                   concept: details.entities?.filter(e => e.type === 'concept') || [],
                 };
                 const typeConfig = {
                   person: { label: 'People', icon: <Users size={11} className="text-blue-400" />, badge: 'bg-blue-500/10 border-blue-500/20 text-blue-300' },
                   organisation: { label: 'Organisations', icon: <Building2 size={11} className="text-emerald-400" />, badge: 'bg-emerald-500/10 border-emerald-500/20 text-emerald-300' },
                   tool: { label: 'Tools', icon: <Wrench size={11} className="text-violet-400" />, badge: 'bg-violet-500/10 border-violet-500/20 text-violet-300' },
                   concept: { label: 'Concepts', icon: <Lightbulb size={11} className="text-amber-400" />, badge: 'bg-amber-500/10 border-amber-500/20 text-amber-300' },
                 };
                 return (
                   <div className="space-y-5">
                     <div className="flex items-center gap-2">
                       <GitBranch size={12} className="text-accent-main" />
                       <h4 className="text-[11px] font-bold text-gray-500 uppercase tracking-widest">Extracted Entities</h4>
                     </div>
                     {Object.entries(byType).map(([type, ents]) => {
                       if (!ents.length) return null;
                       const cfg = typeConfig[type];
                       return (
                         <div key={type} className="space-y-2">
                           <div className="flex items-center gap-1.5">
                             {cfg.icon}
                             <span className="text-[10px] font-bold text-gray-500 uppercase tracking-widest">{cfg.label}</span>
                           </div>
                           <div className="flex flex-wrap gap-2">
                             {ents.map((ent, idx) => (
                               <span key={idx} title={ent.description || ''} className={`px-3 py-1 rounded-full text-[12px] font-medium border ${cfg.badge} cursor-default`}>
                                 {ent.name}
                               </span>
                             ))}
                           </div>
                         </div>
                       );
                     })}
                     {details.relationships?.length > 0 && (
                       <div className="space-y-2">
                         <div className="flex items-center gap-1.5">
                           <GitBranch size={11} className="text-gray-500" />
                           <span className="text-[10px] font-bold text-gray-500 uppercase tracking-widest">Relationships</span>
                         </div>
                         <div className="space-y-1.5">
                           {details.relationships.map((rel, idx) => (
                             <div key={idx} className="flex items-center gap-2 text-[12px] text-gray-400 bg-white/5 rounded-xl px-4 py-2 border border-white/5">
                               <span className="font-semibold text-gray-300">{rel.from}</span>
                               <span className="text-gray-600 italic">{rel.relationship}</span>
                               <span className="font-semibold text-gray-300">{rel.to}</span>
                             </div>
                           ))}
                         </div>
                       </div>
                     )}
                   </div>
                 );
               })()}

               {/* Document Body: Premium Typography */}
               {details?.content && !isVideo && !isPdf && !isDocx && (
                 <div className="space-y-6">
                   <div className="flex items-center gap-2 mb-4">
                     <FileText size={12} className="text-accent-main" />
                     <h4 className="text-[11px] font-bold text-gray-500 uppercase tracking-widest">Source Transcript</h4>
                   </div>
                   <div className="text-[16px] text-gray-300 leading-[1.8] font-normal font-sans p-10 bg-white/5 border border-white/5 rounded-3xl shadow-inner-lg prose prose-invert max-w-none">
                     <ReactMarkdown 
                        components={{
                          h1: ({node, ...props}) => <h1 className="text-2xl font-bold text-white mb-4 mt-6 border-b border-white/10 pb-2" {...props} />,
                          h2: ({node, ...props}) => <h2 className="text-xl font-semibold text-white mb-3 mt-5" {...props} />,
                          h3: ({node, ...props}) => <h3 className="text-lg font-medium text-gray-200 mb-2 mt-4" {...props} />,
                          p: ({node, ...props}) => <p className="mb-4 text-gray-300 leading-relaxed" {...props} />,
                          ul: ({node, ...props}) => <ul className="list-disc pl-6 mb-4 space-y-2" {...props} />,
                          ol: ({node, ...props}) => <ol className="list-decimal pl-6 mb-4 space-y-2" {...props} />,
                          li: ({node, ...props}) => <li className="pl-1" {...props} />,
                          blockquote: ({node, ...props}) => <blockquote className="border-l-4 border-accent-main/50 pl-4 italic text-gray-400 my-4" {...props} />,
                          code: ({node, inline, ...props}) => inline 
                            ? <code className="bg-white/10 rounded px-1.5 py-0.5 text-sm font-mono text-accent-main" {...props} />
                            : <pre className="bg-black/30 rounded-lg p-4 overflow-x-auto border border-white/10 my-4"><code className="text-sm font-mono text-gray-300" {...props} /></pre>
                        }}
                     >
                       {sanitizeContent(details.content)}
                     </ReactMarkdown>
                   </div>
                 </div>
               )}

               {/* Connections */}
               {details?.related_files?.length > 0 && (
                 <div className="space-y-4">
                   <div className="flex items-center gap-2 mb-4">
                     <Layout size={12} className="text-accent-main" />
                     <h4 className="text-[11px] font-bold text-gray-500 uppercase tracking-widest">Linked Dependencies</h4>
                   </div>
                   <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                     {details.related_files.map((file, idx) => (
                       <button 
                         key={idx} 
                         onClick={() => onPreview(file)}
                         className="group flex items-center justify-between px-6 py-4 bg-white/5 rounded-2xl border border-white/5 hover:border-accent-main/40 hover:bg-white/10 transition-all cursor-pointer text-left w-full"
                       >
                         <span className="text-[13px] font-medium text-gray-400 group-hover:text-white transition-colors">{file}</span>
                         <ChevronRight size={14} className="text-gray-600 transition-transform group-hover:translate-x-1" />
                       </button>
                     ))}
                   </div>
                 </div>
               )}
             </div>
          )}
        </div>
      </div>
      <style>{`
        .animate-pulse-subtle { animation: pulse 2s infinite; }
        @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.6; } }
        .shadow-inner-lg { box-shadow: inset 0 2px 20px 0 rgba(0, 0, 0, 0.4); }
      `}</style>
    </div>
  );
}
