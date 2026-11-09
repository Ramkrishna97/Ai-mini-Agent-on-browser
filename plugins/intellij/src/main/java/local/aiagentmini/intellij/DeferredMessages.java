package local.aiagentmini.intellij;

import java.util.ArrayDeque;
import java.util.List;

/** Project/session-local follow-ups; failed or cancelled work never silently advances the queue. */
final class DeferredMessages<T> {
    private final ArrayDeque<T> items = new ArrayDeque<>();

    void add(T item) { items.addLast(item); }
    void restore(T item) { items.addFirst(item); }
    T poll(String status, boolean busy) {
        return !busy && ("idle".equals(status) || "succeeded".equals(status)) ? items.pollFirst() : null;
    }
    void clear() { items.clear(); }
    int size() { return items.size(); }
    List<T> snapshot() { return List.copyOf(items); }
}
