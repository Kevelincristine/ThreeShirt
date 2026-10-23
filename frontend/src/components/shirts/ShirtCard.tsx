interface ShirtCardProps {
  title: string;
  description: string;
  imageUrl?: string;
  imageAlt?: string;
  caption?: string;
}

export function ShirtCard({
  title,
  description,
  imageUrl,
  imageAlt = "",
  caption,
}: ShirtCardProps) {
  return (
    <article aria-label={title} className="shirt-card">
      {imageUrl ? (
        <img className="shirt-card__image" src={imageUrl} alt={imageAlt} />
      ) : null}
      <div className="shirt-card__content">
        <h2>{title}</h2>
        <p>{description}</p>
        {caption ? <p className="shirt-card__caption">{caption}</p> : null}
      </div>
    </article>
  );
}
