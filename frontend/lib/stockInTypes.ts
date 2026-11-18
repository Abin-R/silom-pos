export type StockInType = {
  id: string;
  name: string;
  name_th?: string;
  sort_order?: number;
  active?: boolean;
};

type TypeResponse = {
  ok: boolean;
  json: () => Promise<unknown>;
};

export async function loadStockInTypes(
  fetcher: (url: string) => Promise<TypeResponse>,
  api: string,
): Promise<StockInType[]> {
  const response = await fetcher(`${api}/stock-in-reasons?active=true`);
  if (!response.ok) throw new Error("Unable to load stock-in types");
  const data = await response.json();
  return Array.isArray(data) ? data as StockInType[] : [];
}

export const canSaveStockIn = (lineCount: number, selectedType: string) =>
  lineCount > 0 && !!selectedType;
