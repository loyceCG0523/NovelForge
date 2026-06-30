"use client";

import AppShell from "@/components/AppShell";
import MetricCard from "@/components/MetricCard";

export default function SampleAnalysisPage() {
  return (
    <AppShell
      title="样本分析"
      subtitle="沉淀用户偏好的真人写作风格，后续供生成 Agent 调用"
      actions={<button className="primary-button">导入样本文本</button>}
    >
      <section className="grid-4">
        <MetricCard label="样本库" value="0 篇" note="等待用户上传" />
        <MetricCard label="风格标签" value="预留" note="句长、节奏、对白密度" tone="purple" />
        <MetricCard label="AI 味规避" value="预留" note="模板句式和解释性表达" tone="yellow" />
        <MetricCard label="可用状态" value="未训练" note="后续接 Agent 风格提取" tone="red" />
      </section>

      <section className="grid-2">
        <div className="panel">
          <div className="panel-header">
            <div><div className="panel-title">样本导入区</div><div className="panel-subtitle">后续接入文件上传、文本切片和风格向量化</div></div>
          </div>
          <div className="panel-body form-grid">
            <div className="field"><label>样本类型</label><select><option>用户原创样本</option><option>目标风格参考</option></select></div>
            <div className="field"><label>文本内容</label><textarea placeholder="粘贴样本文本，后续会进入风格分析队列。" /></div>
            <button className="secondary-button">保存为待分析样本</button>
          </div>
        </div>

        <div className="panel">
          <div className="panel-header">
            <div><div className="panel-title">风格画像预留</div><div className="panel-subtitle">这些维度会影响后续章节生成提示词和审校规则</div></div>
          </div>
          <div className="panel-body grid-2">
            <div className="mini-stat"><span>叙事节奏</span><strong>预留</strong></div>
            <div className="mini-stat"><span>对白比例</span><strong>预留</strong></div>
            <div className="mini-stat"><span>句式偏好</span><strong>预留</strong></div>
            <div className="mini-stat"><span>禁用表达</span><strong>预留</strong></div>
          </div>
        </div>
      </section>
    </AppShell>
  );
}
