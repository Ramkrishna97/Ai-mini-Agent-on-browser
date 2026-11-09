package local.aiagentmini.intellij;

import org.junit.Test;
import java.util.List;
import static org.junit.Assert.*;

public class DeferredMessagesTest {
    @Test public void busyModelNeverReceivesAQueuedFollowup() {
        var queue = new DeferredMessages<String>();
        queue.add("next");
        for (String status : List.of("idle", "running", "succeeded")) assertNull(queue.poll(status, true));
        assertNull(queue.poll("running", false));
        assertEquals(1, queue.size());
    }

    @Test public void successfulCompletionReleasesMessagesInOrder() {
        var queue = new DeferredMessages<String>();
        queue.add("first");
        queue.add("second");
        assertEquals("first", queue.poll("succeeded", false));
        assertNull(queue.poll("running", true));
        assertEquals("second", queue.poll("succeeded", false));
        assertNull(queue.poll("idle", false));
    }

    @Test public void failedCancelledAndUnknownStatesDoNotAdvanceTheQueue() {
        var queue = new DeferredMessages<String>();
        queue.add("keep");
        for (String status : List.of("failed", "cancelled", "", "loading")) assertNull(queue.poll(status, false));
        assertEquals(List.of("keep"), queue.snapshot());
    }

    @Test public void failedSubmissionIsRestoredAheadOfLaterMessagesAndNewChatClearsAll() {
        var queue = new DeferredMessages<String>();
        queue.add("first");
        queue.add("second");
        String first = queue.poll("idle", false);
        queue.restore(first);
        assertEquals(List.of("first", "second"), queue.snapshot());
        queue.clear();
        assertNull(queue.poll("succeeded", false));
        assertEquals(0, queue.size());
    }
}
