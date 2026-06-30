export default function EmptyState({ title, description, action }) {
  return (
    <section className="empty-state">
      <div className="empty-mark">N</div>
      <h2>{title}</h2>
      <p>{description}</p>
      {action}
    </section>
  );
}
