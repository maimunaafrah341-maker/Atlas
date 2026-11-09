/** Placeholder cards while products load; announced once as "Loading products…". */
export function CardSkeletons({ count, className }: { count: number; className: string }) {
  return (
    <div role="status" className={className}>
      <span className="sr-only">Loading products…</span>
      {Array.from({ length: count }, (_, index) => (
        <div key={index} aria-hidden="true" className="animate-pulse">
          <div className="aspect-[4/5] bg-tint-sand" />
          <div className="mt-4 h-3 w-1/3 bg-tint-sand" />
          <div className="mt-3 h-4 w-2/3 bg-tint-sand" />
        </div>
      ))}
    </div>
  );
}
