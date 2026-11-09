import { AssistantTeaser, BrandStory, CategoryGrid, Hero } from "../features/home/HomeSections.tsx";
import { SelectedEquipment } from "../features/home/SelectedEquipment.tsx";

/** Concept C homepage: hero, categories, live selected equipment, brand story, assistant concept. */
export function HomePage() {
  return (
    <>
      <Hero />
      <CategoryGrid />
      <SelectedEquipment />
      <BrandStory />
      <AssistantTeaser />
    </>
  );
}
