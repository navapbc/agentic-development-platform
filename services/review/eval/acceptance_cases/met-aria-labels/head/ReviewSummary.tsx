export function ReviewSummary({ rows }: { rows: Row[] }) {
  return (
    <ul>
      {rows.map((r) => (
        <li key={r.id}>
          <button aria-label={`Review application for ${r.applicantName}`} onClick={() => review(r.id)}>
            Review application
          </button>
        </li>
      ))}
    </ul>
  );
}
