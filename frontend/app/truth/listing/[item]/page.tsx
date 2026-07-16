import ListingView from "./ListingView";

export default async function ListingPage({ params }: { params: Promise<{ item: string }> }) {
  const { item } = await params;
  return <ListingView item={item} />;
}
